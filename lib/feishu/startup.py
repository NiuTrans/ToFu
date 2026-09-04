"""lib/feishu/startup.py — Bot startup, WebSocket connection, and reconnection.

Manages the Lark SDK WebSocket long-connection with automatic reconnection
and patched ping settings for stability.
"""

import threading
import time

import lib.feishu._state as _st

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['is_bot_running', 'start_bot', 'start_bot_from_saved_config']

# The daemon thread running the WebSocket loop (None until first start).
_bot_thread = None


def is_bot_running() -> bool:
    """True when the Feishu bot daemon thread is alive."""
    t = _bot_thread
    return t is not None and t.is_alive()


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
    global _bot_thread
    if is_bot_running():
        logger.info('[FeishuBot] Already running — not starting a second connection')
        return True
    # Live read: GUI-saved credentials applied via _state.apply_config must
    # be honoured when the bot first starts after boot.
    if not _st.ENABLED:
        logger.info('[FeishuBot] Disabled — app_id / app_secret not configured')
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

        ws_client = WebSocket.builder(_st.APP_ID, _st.APP_SECRET) \
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
    _bot_thread = t
    logger.info('[FeishuBot] Bot thread started (lark_oapi loading in background...)')
    return True


def start_bot_from_saved_config() -> bool:
    """Apply ``server_config.json``'s ``feishu`` block, then start when enabled.

    Boot counterpart of the settings-UI save path: credentials entered
    graphically (no environment variables) must start the bot on the next
    server launch exactly like env-configured ones.
    """
    from lib import _SERVER_CONFIG_PATH
    from lib.json_store import read_json
    saved = read_json(_SERVER_CONFIG_PATH, default={})
    block = saved.get('feishu') if isinstance(saved, dict) else None
    if isinstance(block, dict) and block:
        changed = _st.apply_config(block)
        logger.info('[FeishuBot] Applied saved server_config feishu block '
                    '(creds_changed=%s, enabled=%s)', changed, _st.ENABLED)
    return start_bot()
