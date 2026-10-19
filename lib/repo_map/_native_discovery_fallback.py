"""Keep canonical rg/walk selection within the native scan's owned lifetime.

The sealed worker execs rg in place: it is a directly joined sibling, never a
grandchild left behind when the stream is cancelled. Native Rust performs the
walk only after rg absence/failure; successful empty output is authoritative.
"""
import json

from . import outline

from lib.log import get_logger

logger = get_logger(__name__)


def negotiate(image, root, deadline, cancellation, limits, *, channel_type, retire, check):
    check(deadline, cancellation)
    process = image.start_rg(root)
    channel = None
    try:
        channel = channel_type(process, deadline, cancellation)
        process.stdin.close()
        output = bytearray()
        while True:
            part = channel.read_output()
            if not part:
                break
            if len(output) + len(part) > limits.path_bytes:
                raise ValueError('repository rg discovery exceeds caller path budget')
            output.extend(part)
        channel.finish(native_status=False)
        paths = ([path.decode('utf8', 'replace') for path in output.split(b'\0') if path]
                 if process.returncode in (0, 1) else None)
        if paths is None:
            from lib.project_mod.config import IGNORE_DIRS
            ignored = sorted(IGNORE_DIRS)
        else:
            ignored = []
        payload = json.dumps(dict(paths=paths, ignored_directories=ignored,
                                  depth_limit=outline._WALK_DEPTH_LIMIT,
                                  file_limit=outline._FILE_DISCOVERY_LIMIT),
                             ensure_ascii=False, separators=(',', ':')).encode('utf8')
        if len(payload) > limits.request_bytes:
            raise ValueError('repository discovery plan exceeds caller request budget')
        return payload
    finally:
        import sys
        primary = sys.exc_info()[1]
        channel_error = None
        try:
            if channel is not None:
                channel.close()
        except BaseException as error:
            logger.debug('negotiate failure handled: %s', type(error).__name__)
            channel_error = error
        finally:
            retire(process, primary or channel_error)
        if channel_error is not None:
            if primary is None:
                raise channel_error
            if hasattr(primary, 'add_note'):
                primary.add_note('rg channel close: ' + type(channel_error).__name__)
