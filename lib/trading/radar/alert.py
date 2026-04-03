"""lib/trading/radar/alert.py — Breaking Event Detection & Urgency Scoring.

Monitors intel cache and market data for sudden changes that require
immediate attention. Designed to be called periodically (e.g., every
5 minutes) from the scheduler.

Alert Types:
  - BREAKING_NEWS: High-impact news from intel cache (keyword-based)
  - MARKET_SHOCK:  Major index moves > ±2% intraday
  - SECTOR_SURGE:  Sector rotates sharply (top/bottom sector > ±3%)
  - FLOW_REVERSAL: Northbound flow reversal > ±50亿
"""

import threading
import time
from datetime import datetime, timedelta

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'check_breaking_alerts',
    'get_pending_alerts',
    'dismiss_alert',
    'ALERT_KEYWORDS',
]

# ── Breaking news keywords (scored by urgency) ──
ALERT_KEYWORDS = {
    # Urgency 3 (highest): Immediate market-moving events
    3: [
        '停火', '开战', '制裁', '崩盘', '熔断', '暴跌', '暴涨', '黑天鹅',
        '降准', '降息', '加息', '紧急', '突发', '重大', 'Trump', '特朗普',
        '关税', 'tariff', '战争', 'war', '核', '地震', '疫情',
    ],
    # Urgency 2: Important but less immediate
    2: [
        '央行', '证监会', '监管', '退市', '涨停', '跌停', '利好', '利空',
        '重组', '并购', '违约', '暴雷', 'GDP', 'CPI', 'PMI', '就业',
        '美联储', 'Fed', '欧央行', '日央行', 'OPEC',
    ],
    # Urgency 1: Notable but routine
    1: [
        '北向资金', '外资', '政策', '改革', '调控', '放松', '收紧',
        '板块', '轮动', '热点', '龙头', '成交额', '放量', '缩量',
    ],
}

# ── In-memory alert queue ──
_alerts = []
_alerts_lock = threading.Lock()
_last_check_ts = 0
_CHECK_COOLDOWN = 300  # 5 min between checks


def check_breaking_alerts(db=None):
    """Scan intel cache + market data for breaking events.

    Returns list of new alert dicts. Also stores them in the in-memory queue.

    Args:
        db: Database connection (optional — if None, skips DB-based checks)
    """
    global _last_check_ts
    now = time.time()
    if now - _last_check_ts < _CHECK_COOLDOWN:
        return []
    _last_check_ts = now

    new_alerts = []

    # ── 1. Check recent intel for breaking keywords ──
    if db:
        try:
            cutoff = (datetime.now() - timedelta(hours=2)).strftime('%Y-%m-%d %H:%M:%S')
            recent = db.execute(
                "SELECT id, title, summary, category, fetched_at "
                "FROM trading_intel_cache WHERE fetched_at >= ? "
                "ORDER BY fetched_at DESC LIMIT 100",
                (cutoff,)
            ).fetchall()

            for row in recent:
                item = dict(row)
                text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
                max_urgency = 0
                matched_kw = ''

                for urgency, keywords in ALERT_KEYWORDS.items():
                    for kw in keywords:
                        if kw.lower() in text:
                            if urgency > max_urgency:
                                max_urgency = urgency
                                matched_kw = kw

                if max_urgency >= 2:  # Only alert on urgency >= 2
                    alert_id = f"news_{item.get('id', '')}_{max_urgency}"
                    if not _alert_exists(alert_id):
                        alert = {
                            'id': alert_id,
                            'type': 'BREAKING_NEWS',
                            'urgency': max_urgency,
                            'title': item.get('title', '')[:100],
                            'detail': item.get('summary', '')[:300],
                            'keyword': matched_kw,
                            'category': item.get('category', ''),
                            'timestamp': item.get('fetched_at', ''),
                            'status': 'pending',
                        }
                        new_alerts.append(alert)
        except Exception as e:
            logger.warning('[Alert] Intel scan failed: %s', e, exc_info=True)

    # ── 2. Check major index moves ──
    try:
        from lib.trading.market import fetch_major_indices
        indices = fetch_major_indices()
        for idx in indices:
            pct = idx.get('pct', 0)
            if isinstance(pct, (int, float)) and abs(pct) >= 2.0:
                alert_id = f"shock_{idx.get('secid', '')}_{datetime.now().strftime('%Y%m%d')}"
                if not _alert_exists(alert_id):
                    direction = '暴涨' if pct > 0 else '暴跌'
                    alert = {
                        'id': alert_id,
                        'type': 'MARKET_SHOCK',
                        'urgency': 3 if abs(pct) >= 3.0 else 2,
                        'title': f"{idx.get('name', '')} {direction} {abs(pct):.2f}%",
                        'detail': f"当前价 {idx.get('price', '')}, 成交额 {idx.get('amount', '')}",
                        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        'status': 'pending',
                    }
                    new_alerts.append(alert)
    except Exception as e:
        logger.debug('[Alert] Market index check failed: %s', e, exc_info=True)

    # ── Store new alerts ──
    if new_alerts:
        with _alerts_lock:
            _alerts.extend(new_alerts)
            # Keep only last 100 alerts
            if len(_alerts) > 100:
                _alerts[:] = _alerts[-100:]
        logger.info('[Alert] %d new breaking alerts detected', len(new_alerts))

    return new_alerts


def get_pending_alerts():
    """Return all pending (un-dismissed) alerts, sorted by urgency desc."""
    with _alerts_lock:
        pending = [a for a in _alerts if a.get('status') == 'pending']
    pending.sort(key=lambda x: x.get('urgency', 0), reverse=True)
    return pending


def dismiss_alert(alert_id):
    """Mark an alert as dismissed."""
    with _alerts_lock:
        for a in _alerts:
            if a.get('id') == alert_id:
                a['status'] = 'dismissed'
                return True
    return False


def _alert_exists(alert_id):
    """Check if alert already exists (dedup)."""
    with _alerts_lock:
        return any(a.get('id') == alert_id for a in _alerts)
