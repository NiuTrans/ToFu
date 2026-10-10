"""CLI entry point — ``python -m lib.desktop_agent [args]``.

Kept separate from ``__init__`` so that a bare ``import lib.desktop_agent``
never parses argv or starts the polling loop.
"""

from lib.desktop_agent._run import main
from lib.log import get_logger

logger = get_logger(__name__)


if __name__ == '__main__':
    main()
