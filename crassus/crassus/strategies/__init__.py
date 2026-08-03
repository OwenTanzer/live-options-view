"""Strategy implementations.

Importing this package registers every strategy in `strategy.REGISTRY`.
"""

from . import momentum_qqq  # noqa: F401
from . import reddit_sentiment  # noqa: F401
from . import smoke  # noqa: F401
from . import trump_whisperer  # noqa: F401
from . import vwap_breakout  # noqa: F401
from . import vwap_fade  # noqa: F401

__all__ = ["momentum_qqq", "reddit_sentiment", "smoke", "trump_whisperer", "vwap_breakout", "vwap_fade"]
