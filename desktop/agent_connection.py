"""Shared launcher connection lifecycle; no UI or server imports.

Entry points start/stop/restart own one poll worker per state. Relay setup and
attachment resume are lazy; local full-app control never discovers remote routes.
"""
import threading


def _lock(state):
    return state.setdefault('_connection_lock', threading.RLock())


def resume(url, secret, log):
    if url:
        try:
            from lib.desktop_agent._pair import resume_attachment
            return resume_attachment(url, secret, log=log)
        except Exception as exc:
            log('Attachment resume probe skipped: %s' % exc)
    return url, secret


def ensure_relay(state, log, attach_handler=None):
    """Create at most one broker. Full local control never calls this."""
    with _lock(state):
        if state.get('browser_relay') is not None:
            return
        try:
            from lib.desktop_agent._browser_relay import BrowserRelay

            def urls():
                candidates = [state.get('url') or '']
                try:
                    from lib.desktop_agent.config import load_config
                    candidates.extend(load_config().get('attach_candidates') or [])
                except Exception as exc:
                    log('Browser-relay candidate read failed: %s' % exc)
                return candidates

            relay = BrowserRelay(urls, log=log, attach_handler=attach_handler,
                                 attach_state=lambda: bool(state.get('url')))
            if relay.start():
                state['browser_relay'] = relay
        except Exception as exc:
            log('Browser relay unavailable: %s' % exc)


def start(state, perms, log, *, remote=True):
    with _lock(state):
        thread = state.get('thread')
        if thread and thread.is_alive():
            return False
        url, secret = state.get('url') or '', state.get('secret') or ''
        if not url:
            state['last_status'] = {'state': 'unconfigured'}
            state['enabled'] = False
            return False
        from lib.desktop_agent import run_agent
        stop_event = threading.Event()
        state['stop'] = stop_event
        state['error'] = None

        def on_status(status):
            state['last_status'] = status
            log('Link status: %s' % status)

        def route_repair():
            current = state.get('url') or ''
            credential = state.get('secret') or ''
            new_url, new_secret = resume(current, credential, log)
            if stop_event.is_set():
                return None
            if new_url and (new_url, new_secret) != (current, credential):
                state['url'], state['secret'] = new_url, new_secret
                state['server_url'] = new_url
                return new_url, new_secret
            return None

        def loop():
            try:
                run_agent(url, perms, poll_interval=1.0,
                          bridge_secret=secret, stop_event=stop_event,
                          on_status=on_status,
                          route_repair=route_repair if remote else None,
                          browser_relay=state.get('browser_relay') if remote else None)
            except Exception as exc:
                state['error'] = str(exc)
                on_status({'state': 'error', 'detail': str(exc)})
                log('Agent loop crashed: %s' % exc)
            finally:
                state['enabled'] = False

        thread = threading.Thread(target=loop, daemon=True, name='tofu-desktop-agent')
        state['thread'] = thread
        state['enabled'] = True
        thread.start()
        return True


def stop(state, log, *, close_relay=True):
    """Bounded join; retain a live worker handle rather than spawn a duplicate."""
    with _lock(state):
        event = state.get('stop')
        if event is not None:
            event.set()
        state['enabled'] = False
        thread = state.get('thread')
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)
        if close_relay:
            relay = state.pop('browser_relay', None)
            if relay is not None:
                relay.close()
        stopped = not thread or not thread.is_alive()
        if not stopped:
            log('Agent is still stopping; refusing a second polling worker')
        return stopped


def restart(state, perms, log):
    with _lock(state):
        if stop(state, log, close_relay=False):
            return start(state, perms, log)
        return False
