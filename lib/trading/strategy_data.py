"""lib/trading/strategy_data.py — Built-in strategy definitions, seeding, and performance tracking.

Extracted from the monolithic lib/trading.py to keep each sub-module focused on one domain.
"""

import json
from collections import defaultdict
from datetime import datetime

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'BUILTIN_STRATEGIES',
    'BUILTIN_STRATEGY_GROUPS',
    'seed_builtin_strategies',
    'seed_builtin_strategy_groups',
    'record_decision',
    'save_decision_history',
    'get_strategy_performance_summary',
    'record_strategy_performance',
]


# ═══════════════════════════════════════════════════════════
#  Built-in Strategy Definitions
# ═══════════════════════════════════════════════════════════

BUILTIN_STRATEGIES = [
    # ── Risk Control ──
    {
        'name': '动态止损线',
        'type': 'risk_control',
        'logic': '当任一持仓标的回撤超过预设阈值（默认-15%）时触发卖出信号。回撤从近60日最高点计算，避免在市场剧烈波动时遭受过大损失。',
        'scenario': '适用于所有持仓，尤其是波动较大的权益类资产。在熊市或系统性风险事件中保护本金。',
        'assets': '全部权益类资产',
        'params': {'drawdown_threshold': -15, 'lookback_days': 60},
    },
    {
        'name': '仓位上限控制',
        'type': 'risk_control',
        'logic': '单只标的持仓比例不得超过总资产的35%，单一行业主题ETF合计不超过50%。当超限时自动生成减仓建议，将超额部分转移到货币型产品或债券产品。',
        'scenario': '防止过度集中投资于单一标的或行业。适用于组合管理的全过程。',
        'assets': '全部',
        'params': {'single_max_pct': 35, 'sector_max_pct': 50},
    },
    {
        'name': '波动率自适应仓位',
        'type': 'risk_control',
        'logic': '根据市场波动率（VIX等价指标或沪深300 20日波动率）动态调整权益仓位。波动率>25%时仓位降至60%，>35%降至40%，<15%时可加仓至90%。',
        'scenario': '适用于中长期投资组合。在市场不确定性增大时自动降低风险暴露。',
        'assets': '权益类资产',
        'params': {'vol_high': 35, 'vol_medium': 25, 'vol_low': 15},
    },
    # ── Buy Signal ──
    {
        'name': '均线金叉买入',
        'type': 'buy_signal',
        'logic': '当标的价格的10日均线上穿30日均线（金叉）时，视为短期趋势反转向上的信号。结合成交量（如ETF可参考）确认有效性。仅在大盘20日均线在60日均线之上时触发。',
        'scenario': '适用于趋势跟随型投资。在市场企稳反弹初期捕捉入场时机。',
        'assets': '宽基ETFETF、行业ETF',
        'params': {'fast_ma': 10, 'slow_ma': 30, 'market_ma_fast': 20, 'market_ma_slow': 60},
    },
    {
        'name': '估值低位买入',
        'type': 'buy_signal',
        'logic': '当指数PE或PB处于近5年历史分位数30%以下时，分批买入对应指数ETF。分位数20%以下时加大买入力度（定投金额翻倍）。',
        'scenario': '适用于长期价值投资。在市场低估时逐步建仓，具有较高的安全边际。',
        'assets': '沪深300、中证500、创业板等宽基ETFETF',
        'params': {'pe_low': 30, 'pe_very_low': 20, 'lookback_years': 5},
    },
    {
        'name': '大幅回调抄底',
        'type': 'buy_signal',
        'logic': '当标的价格连续3日累计跌幅超过-5%，且RSI(14)<30（超卖区间），触发抄底买入。单次买入金额为可用现金的10%-15%，避免一次性重仓。',
        'scenario': '适用于优质标的遭遇短期非理性下跌时。需配合基本面判断排除标的本身问题。',
        'assets': '长期业绩优秀的主动管理产品',
        'params': {'cumulative_drop': -5, 'rsi_threshold': 30, 'buy_pct': 0.12},
    },
    # ── Sell Signal ──
    {
        'name': '目标收益止盈',
        'type': 'sell_signal',
        'logic': '当持仓收益率达到预设目标（默认+25%）时，卖出50%仓位锁定利润。剩余仓位设置移动止盈（从最高收益回撤8%则全部卖出）。',
        'scenario': '适用于有明确收益预期的投资。避免贪婪导致利润回吐。',
        'assets': '全部',
        'params': {'target_return': 25, 'partial_sell_pct': 50, 'trailing_stop': 8},
    },
    {
        'name': '估值高位止盈',
        'type': 'sell_signal',
        'logic': '当指数PE处于近5年分位数70%以上时开始分批减仓（每次减20%），90%以上清仓。配合投资经理换手率异常升高作为辅助卖出信号。',
        'scenario': '适用于指数ETF的系统性卖出。在市场过热时逐步退出。',
        'assets': '指数ETF、行业主题ETF',
        'params': {'pe_high': 70, 'pe_very_high': 90, 'sell_batch_pct': 20},
    },
    # ── Allocation ──
    {
        'name': '核心-卫星配置',
        'type': 'allocation',
        'logic': '核心仓位（60%-70%）配置宽基ETFETF（沪深300+中证500），提供稳定的市场收益。卫星仓位（30%-40%）配置行业主题ETF，追求超额收益。每季度评估一次卫星仓位的行业选择。',
        'scenario': '适用于追求稳健增长的中长期投资者。核心提供安全垫，卫星追求进攻性。',
        'assets': '沪深300、中证500作为核心；科技、医药、新能源等作为卫星',
        'params': {'core_pct': 65, 'satellite_pct': 35, 'rebalance_freq': 'quarterly'},
    },
    {
        'name': '股债平衡策略',
        'type': 'allocation',
        'logic': '维持股债比例在6:4到7:3之间。当股市PE分位数>60%时调至5:5，<30%时调至8:2。债券部分选择中短债ETF以降低利率风险。每月检查偏离度，超过5%触发再平衡。',
        'scenario': '适用于风险偏好中等的投资者。通过股债对冲降低整体波动。',
        'assets': '股票型/混合型资产 + 债券型产品',
        'params': {'stock_pct_normal': 65, 'bond_pct_normal': 35, 'deviation_threshold': 5},
    },
    {
        'name': '全天候配置',
        'type': 'allocation',
        'logic': '借鉴桥水全天候思路：30%股票型产品+40%长期债券产品+15%中期债券产品+7.5%黄金QDII+7.5%商品ETF。季度再平衡一次，任何资产偏离目标5%以上立即再平衡。',
        'scenario': '适用于追求全天候稳定回报的投资者。在任何经济环境下都有一定适应性。',
        'assets': '沪深300+中证全债+黄金ETF+商品ETF',
        'params': {'stock': 30, 'long_bond': 40, 'mid_bond': 15, 'gold': 7.5, 'commodity': 7.5},
    },
    # ── Timing ──
    {
        'name': '定投增强策略',
        'type': 'timing',
        'logic': '基础定投+智能加减码：每月固定日期定投基础金额。当标的价格低于20日均线时加投50%（越低越买），高于20日均线超过5%时减投50%。结合PE分位数调整基础金额。',
        'scenario': '适用于工薪族的长期积累。通过均值回归的特性在低位多投、高位少投。',
        'assets': '宽基ETFETF',
        'params': {'base_amount': 1000, 'ma_period': 20, 'overvalue_reduce': 0.5, 'undervalue_boost': 0.5},
    },
    {
        'name': '网格交易策略',
        'type': 'timing',
        'logic': '在标的价格建立网格：以当前净值为中心，每下跌3%买入一份（总资金的8%），每上涨3%卖出一份。设定网格边界（-15%~+15%），超出边界暂停交易。',
        'scenario': '适用于震荡市场中的波段操作。需要足够的资金保证在下跌时有足够的买入弹药。',
        'assets': '波动适中的混合型资产或宽基ETF',
        'params': {'grid_pct': 3, 'per_grid_pct': 8, 'max_boundary': 15},
    },
    # ── Observation ──
    {
        'name': '北向资金跟踪',
        'type': 'observation',
        'logic': '监控北向资金（陆股通）净流入数据。连续3日净流入超50亿视为积极信号，连续3日净流出超30亿视为警戒信号。结合行业层面的北向资金流向判断板块偏好变化。',
        'scenario': '作为辅助判断指标。北向资金被视为"聪明钱"，其动向可反映外资对A股市场的态度。',
        'assets': '受外资偏好影响的标的（消费、医药、科技龙头）',
        'params': {'inflow_threshold': 50, 'outflow_threshold': -30, 'consecutive_days': 3},
    },
    {
        'name': '市场情绪监测',
        'type': 'observation',
        'logic': '综合监测：融资余额变化率、新ETF发行规模、散户开户数、媒体情绪指标。当多项指标同时处于极端（极度贪婪或极度恐惧）时输出预警。极端恐惧时往往是买入机会，极端贪婪时注意控制仓位。',
        'scenario': '逆向投资的辅助工具。情绪极端时往往是市场转折点的前兆。',
        'assets': '全部',
        'params': {'greed_threshold': 80, 'fear_threshold': 20},
    },
]

BUILTIN_STRATEGY_GROUPS = [
    {
        'name': '保守稳健型',
        'description': '注重风险控制和稳健配置，适合低风险偏好投资者',
        'strategy_names': ['股债平衡策略', '全天候配置', '目标收益止盈', '最大回撤止损', '仓位管理策略'],
        'risk_level': 'low',
    },
    {
        'name': '稳健均衡型',
        'description': '平衡收益与风险，适合大多数投资者',
        'strategy_names': ['核心-卫星配置', '定投增强策略', '估值低位买入', '估值高位止盈', '北向资金跟踪', '仓位管理策略'],
        'risk_level': 'medium',
    },
    {
        'name': '积极进取型',
        'description': '追求超额收益，容忍较高波动',
        'strategy_names': ['均线金叉买入', '大幅回调抄底', '网格交易策略', '核心-卫星配置', '市场情绪监测', '最大回撤止损'],
        'risk_level': 'high',
    },
    {
        'name': '量化择时型',
        'description': '以技术分析和量化信号为主导的策略组合',
        'strategy_names': ['均线金叉买入', '网格交易策略', '北向资金跟踪', '市场情绪监测', '仓位管理策略'],
        'risk_level': 'medium',
    },
]


# ═══════════════════════════════════════════════════════════
#  Seeding Functions
# ═══════════════════════════════════════════════════════════

def seed_builtin_strategies(db):
    """Insert built-in strategies if they don't exist yet."""
    existing = db.execute('SELECT name FROM trading_strategies WHERE source=?', ('builtin',)).fetchall()
    existing_names = {r['name'] for r in existing}
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    inserted = 0

    for s in BUILTIN_STRATEGIES:
        if s['name'] in existing_names:
            continue
        db.execute(
            '''INSERT INTO trading_strategies (name, type, status, logic, scenario, assets, result, source, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (s['name'], s['type'], 'active', s['logic'],
             s['scenario'], s['assets'], '', 'builtin', now, now)
        )
        inserted += 1

    if inserted > 0:
        db.commit()
        logger.info('Seeded %d built-in strategies', inserted)
    return inserted


def seed_builtin_strategy_groups(db):
    """Create default strategy groups from built-in strategies."""
    existing_groups = db.execute('SELECT name FROM trading_strategy_groups').fetchall()
    if len(existing_groups) > 0:
        return 0  # Already seeded

    strategies = db.execute("SELECT id, name, type FROM trading_strategies WHERE source='builtin' AND status='active'").fetchall()
    if not strategies:
        return 0

    by_type = defaultdict(list)
    for s in strategies:
        by_type[s['type']].append(s['id'])

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    groups = [
        {
            'name': '保守稳健型',
            'description': '注重风险控制和稳健配置，适合低风险偏好投资者。以股债平衡+全天候配置为核心，配合严格的止损止盈机制。',
            'strategy_ids': (
                by_type.get('risk_control', [])[:3] +
                [s['id'] for s in strategies if s['name'] in ['股债平衡策略', '全天候配置', '目标收益止盈']]
            ),
            'risk_level': 'low',
        },
        {
            'name': '稳健均衡型',
            'description': '平衡收益与风险。采用核心-卫星策略为主，配合定投增强和估值买卖信号。适合大多数投资者。',
            'strategy_ids': (
                by_type.get('risk_control', [])[:2] +
                [s['id'] for s in strategies if s['name'] in ['核心-卫星配置', '定投增强策略', '估值低位买入', '估值高位止盈', '北向资金跟踪']]
            ),
            'risk_level': 'medium',
        },
        {
            'name': '积极进取型',
            'description': '追求超额收益，容忍较高波动。以趋势跟随+抄底策略为核心，配合网格交易进行波段操作。',
            'strategy_ids': (
                by_type.get('risk_control', [])[:1] +
                [s['id'] for s in strategies if s['name'] in ['均线金叉买入', '大幅回调抄底', '网格交易策略', '核心-卫星配置', '市场情绪监测']]
            ),
            'risk_level': 'high',
        },
        {
            'name': '全策略组合',
            'description': '包含所有可用策略，由AI根据市场环境动态选择激活。适合希望全面覆盖各种市场状况的高级用户。',
            'strategy_ids': [s['id'] for s in strategies],
            'risk_level': 'medium',
        },
    ]

    inserted = 0
    for g in groups:
        try:
            db.execute(
                '''INSERT INTO trading_strategy_groups (name, description, strategy_ids, risk_level, created_at, updated_at)
                   VALUES (?,?,?,?,?,?)''',
                (g['name'], g['description'], json.dumps(g['strategy_ids']),
                 g['risk_level'], now, now)
            )
            inserted += 1
        except Exception as e:
            logger.error('Error inserting strategy group %s: %s', g['name'], e, exc_info=True)

    if inserted > 0:
        db.commit()
        logger.info('Seeded %d default strategy groups', inserted)
    return inserted


# ═══════════════════════════════════════════════════════════
#  Performance Tracking
# ═══════════════════════════════════════════════════════════

def record_strategy_performance(db, strategy_id, period_start, period_end,
                                 return_pct, benchmark_return_pct=0,
                                 max_drawdown=0, sharpe_ratio=None,
                                 win_rate=None, trade_count=0,
                                 source='live', detail_json=None):
    """Record performance metrics for a strategy over a time period."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    db.execute(
        '''INSERT INTO trading_strategy_performance
           (strategy_id, period_start, period_end, return_pct, benchmark_return_pct,
            max_drawdown, sharpe_ratio, win_rate, trade_count, source, detail_json, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
        (strategy_id, period_start, period_end, return_pct, benchmark_return_pct,
         max_drawdown, sharpe_ratio, win_rate, trade_count, source,
         json.dumps(detail_json or {}), now)
    )
    db.commit()


def get_strategy_performance_summary(db, strategy_id=None):
    """Get aggregated performance summary for strategies."""
    if strategy_id:
        rows = db.execute(
            'SELECT * FROM trading_strategy_performance WHERE strategy_id=? ORDER BY period_end DESC',
            (strategy_id,)
        ).fetchall()
    else:
        rows = db.execute(
            'SELECT * FROM trading_strategy_performance ORDER BY period_end DESC'
        ).fetchall()

    if not rows:
        return {'total_records': 0}

    returns = [r['return_pct'] for r in rows]
    return {
        'total_records': len(rows),
        'avg_return': round(sum(returns) / len(returns), 2),
        'best_return': round(max(returns), 2),
        'worst_return': round(min(returns), 2),
        'avg_drawdown': round(sum(r['max_drawdown'] for r in rows) / len(rows), 2),
        'win_count': sum(1 for r in rows if r['return_pct'] > 0),
        'loss_count': sum(1 for r in rows if r['return_pct'] <= 0),
        'records': [dict(r) for r in rows[:20]],
    }


def record_decision(db, batch_id, strategy_group_id, strategy_group_name,
                     briefing_content, recommendation_content, trades_json):
    """Record a decision for history tracking."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    db.execute(
        '''INSERT INTO trading_decision_history
           (batch_id, strategy_group_id, strategy_group_name, briefing_content,
            recommendation_content, trades_json, status, created_at)
           VALUES (?,?,?,?,?,?,?,?)''',
        (batch_id, strategy_group_id, strategy_group_name or '',
         briefing_content, recommendation_content, json.dumps(trades_json), 'generated', now)
    )
    db.commit()


# Aliases for backward-compatibility
save_decision_history = record_decision
