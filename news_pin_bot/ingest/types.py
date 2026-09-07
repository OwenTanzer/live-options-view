"""Shared shapes passed from ingest sources -> scorer -> pin engine -> bot."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Headline:
    source: str            # "alpaca" | "finnhub" | "edgar" | "gdelt" | "reddit" | "trump"
    external_id: str | None
    symbols: list[str]
    headline: str
    summary: str
    url: str
    published_at: float    # unix ts
    ingested_at: float = field(default=0.0)
