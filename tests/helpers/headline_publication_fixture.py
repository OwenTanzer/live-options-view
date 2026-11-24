"""Produce the real publisher's archive envelope entirely in temporary storage."""
import dataclasses
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_news_support import feeds, fixed, no_sleep, at
from test_news_publish import qualified
from market_data.news.archive import LocalObjectStore, collect_once
from market_data.news.reader import load_archive
from market_data.news.views import build_views
from market_data.news.publish import unavailable_prices, validate_generation

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    store = LocalObjectStore(root / "store")
    clock = fixed("2026-11-09T10:05:00Z")
    collect_once(store, root / "spool", feeds(), clock, commit_clock=clock, sleep=no_sleep)
    archive = load_archive(store)
    obs = [dict(o, title="NVDA guides higher" if i == 0 else "Rates fall 25 bp",
                excerpt="", categories=[]) for i, o in enumerate(archive.observations)]
    files = build_views(dataclasses.replace(archive, observations=obs), unavailable_prices(),
                        as_of=at("2026-11-09T16:30:00Z"), tolerance_s=120,
                        data_label="PILOT news: prices unavailable (no price input connected)")
    validate_generation(files)
    print(json.dumps({k:json.loads(v) for k,v in qualified(files).items()
                      if k.endswith("/index.json") or "/archive/all/" in k}))
