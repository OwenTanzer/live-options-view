"""Hourly next-hour crypto price predictions, as One Clock observations.

Estimates only — origin ``synthetic``, retained as historical
evidence when their target hour passes. See
``docs/specs/CRYPTO_PREDICTIONS.md``.
"""

from __future__ import annotations

__all__ = ["estimator", "predictions", "producer", "provenance", "publish", "cli"]
