"""Strategy implementations.

Importing this package registers every strategy in `strategy.REGISTRY`.

The P1 smoke strategy, `reddit_sentiment_qqq`, `trump_whisperer_qqq`, and
`momentum_qqq` live here. The six dip/rip/cheap-ATM strategies are P3 work:
the MOO-24 contract explicitly defers rolling out every strategy before one
complete loop is demonstrably alive.
"""

from . import gated_strangle  # noqa: F401
from . import flow_skew  # noqa: F401
from . import canopus_down_day  # noqa: F401
from . import looking_glass_straddle  # noqa: F401
from . import me_bots  # noqa: F401
from . import max_pain  # noqa: F401
from . import max_pain_newton  # noqa: F401
from . import max_pain_episodes  # noqa: F401
from . import momentum_puts_only  # noqa: F401
from . import momentum_qqq  # noqa: F401
from . import oi_associator  # noqa: F401
from . import oi_skew  # noqa: F401
from . import phelps_pure  # noqa: F401
from . import phelps_variants  # noqa: F401
from . import put_call_ratio  # noqa: F401
from . import reddit_sentiment  # noqa: F401
from . import smoke  # noqa: F401
from . import sma200  # noqa: F401
from . import trump_whisperer  # noqa: F401
from . import trump_oi_skew  # noqa: F401

__all__ = [
    "smoke",
    "sma200",
    "reddit_sentiment",
    "trump_whisperer",
    "momentum_qqq",
    "momentum_puts_only",
    "oi_skew",
    "flow_skew",
    "oi_associator",
    "max_pain",
    "max_pain_episodes",
    "put_call_ratio",
    "canopus_down_day",
    "looking_glass_straddle",
    "phelps_variants",
    "phelps_pure",
]
