"""lib/feishu/startup.py — Bot startup, WebSocket connection, and reconnection.

Manages the Lark SDK WebSocket long-connection with automatic reconnection
and patched ping settings for stability.
"""

import threading
import time

from lib.feishu._state import APP_ID, APP_SECRET, ENABLED

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['start_bot']


def _patch_websockets_ping_settings():
    """Patch websockets.connect defaults to increase ping tolerance.

    The lark_oapi SDK calls websockets.connect() with no ping arguments,
    so websockets uses its defaults: ping_interval=20s, ping_timeout=20s.
    This is too aggressive for long-lived Feishu connections — network hiccups
    or CPU-heavy tool execution cause ping timeouts that kill the connection
    with error 1011 ("keepalive ping timeout; no close frame received").

    The SDK has its OWN application-level ping at 120s intervals, so the
    websockets-level ping is redundant. We disable the websockets ping timeout
    (set ping_timeout=None) but keep ping_interval to maintain keepalive
    traffic through proxies. This prevents spurious disconnects while still
    allowing the SDK's built-in reconnection to handle real outages.
    """
    import websockets
    _original_connect = websockets.connect

    class _PatchedConnect(_original_connect.__class__
                          if isinstance(_original_connect, type)
                          else type(_original_connect)):
        pass

    def _patched_connect(*args, **kwargs):
        kwargs.setdefault('ping_interval', 30)
        kwargs.setdefault('ping_timeout', None)
        return _original_connect(*args, **kwargs)

    try:
        websockets.connect = _patched_connect
        logger.debug('[FeishuBot] Patched websockets.connect: ping_timeout=None')
    except Exception as e:
        logger.warning('[FeishuBot] Failed to patch websockets ping: %s', e, exc_info=True)


def start_bot() -> bool:
    """Start the Feishu bot on a background daemon thread.

    Returns True if the bot was started, False if disabled/missing credentials.
    """
    if not ENABLED:
        logger.info('[FeishuBot] Disabled — FEISHU_APP_ID / FEISHU_APP_SECRET not set')
        return False

    def _run():
        import lark_oapi as lark
        from lark_oapi.adapter.websocket import WebSocket

        from lib.feishu.events import handle_menu_event, handle_message_event

        _patch_websockets_ping_settings()

        event_handler = lark.EventDispatcherHandler.builder(
            '', ''  # verification token, encrypt key — unused with WS
        ).register_p2_im_message_receive_v1(
            handle_message_event
        ).register_p1_application_bot_menu_v6(
            handle_menu_event
        ).build()

        ws_client = WebSocket.builder(APP_ID, APP_SECRET) \
            .event_handler(event_handler) \
            .log_level(lark.LogLevel.INFO) \
            .build()

        MAX_BACKOFF = 60
        consecutive_failures = 0

        while True:
            try:
                logger.info(
                    '[FeishuBot] Connecting via WebSocket (attempt #%d)...',
                    consecutive_failures + 1,
                )
                ws_client.start()
                # start() blocks until disconnected
                consecutive_failures = 0  # reset on clean exit
            except KeyboardInterrupt:
                logger.info('[FeishuBot] Interrupted — shutting down')
                break
            except Exception as e:
                consecutive_failures += 1
                logger.error(
                    '[FeishuBot] WebSocket error (attempt #%d): %s',
                    consecutive_failures, e, exc_info=True,
                )

            # ── Exponential backoff with jitter ──
            import random
            base_delay = min(2 ** consecutive_failures, MAX_BACKOFF)
            actual_delay = base_delay + random.uniform(0, 2)
            logger.info(
                '[FeishuBot] Reconnecting in %.1fs (attempt #%d)...',
                actual_delay, consecutive_failures + 1,
            )
            time.sleep(actual_delay)

    t = threading.Thread(target=_run, daemon=True, name='feishu-bot')
    t.start()
    logger.info('[FeishuBot] Bot thread started (lark_oapi loading in background...)')
    return True
