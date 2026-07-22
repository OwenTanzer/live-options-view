"""Crassus / Golden Goose -- an automated trading-bot runtime for Options View.

A thin Python bot runtime and client library, not a network-facing service.
The loop is: observe the market, produce a decision, validate it, submit it,
reconcile against the server, append an audit record, repeat.

Both names refer to the same system (MOO-24) and are interchangeable.
"""

__version__ = "0.1.0"

from . import audit, clock, client, config, market, strategy  # noqa: F401

__all__ = ["audit", "clock", "client", "config", "market", "strategy", "__version__"]
