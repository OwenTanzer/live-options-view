"""Build hourly crypto predictions from the cached 1h bars.

The producer reads the candle cache's 1h interval files (see
:mod:`market_data.crypto_candle_cache.cache`), estimates the next
hour's close per coin with :mod:`estimator`, and writes one
``crypto/predictions/<COIN>.json`` file per coin plus a
``snapshot_provenance.v1`` sidecar (see :mod:`provenance`).

Rules:

- Only *completed* 1h bars enter the estimator window. The
  still-forming latest bar (close in the future at fetch time) is
  excluded with :func:`one_clock.split_forming`, exactly as the
  candle reader excludes it from selection — an estimate must
  never rest on a bar that has not closed.
- One prediction per coin per run, for the target hour starting at
  the last completed bar's close. Re-running within the same hour
  produces the same id at revision 1 (same estimate — the window
  is unchanged); a re-run after new bars close produces a new id.
- Retention, not retraction: a prediction whose target hour has
  passed stays in the file with its value intact, as historical
  evidence against which the prediction can be judged — it is
  never published as a revision-2 null row. Past predictions
  are carried forward for 7 days, then pruned (a bound on file
  size, not a retraction). A reader judges a passed prediction
  by its ``eventAt`` / ``availableAt`` and its value's
  ``target_start`` / ``target_end``. A producer run that lands
  after its target hour has already passed publishes the
  estimate as historical evidence in the same file: it was
  never selectable during its hour (its ``availableAt`` is
  after the hour ended), and its value is retained so the
  judgement can still be made.
- A coin with no 1h file — CoinGecko-labelled coins have a 1d
- A coin with no 1h file — CoinGecko-labelled coins have a 1d
  base only — produces no prediction file. Its provenance entry
  says ``"absent"`` with the reason; a price is never
  interpolated from daily bars.
- A coin whose 1h file exists but holds fewer than 2 completed
  bars likewise produces no estimate, with the reason recorded.
- BTC's 1h bars are loaded once and passed to every other coin as
  the correlation input; a missing BTC file means the other coins
  are estimated without the correlation signal (the basis says
  so), never blocked.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from market_data.crypto_candle_cache import cache as cache_mod
from market_data.crypto_candle_cache import coins as coins_mod
from market_data.crypto_candle_cache import one_clock as oc

from . import estimator as est_mod
from . import predictions as pred_mod

UTC = timezone.utc
RETENTION_MS = 7 * 24 * 60 * 60 * 1000


def _parse_time(value):
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def _hour_start_ms(moment):
    moment = moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    return int(moment.timestamp() * 1000)


def load_completed_1h(cache_dir, symbol, now_ms):
    """Completed 1h bars for a coin, or None when the file is absent.

    Returns ``(bars, source)`` with forming bars removed. Raises
    ``FileNotFoundError`` when no 1h file exists for the coin.
    """
    path = cache_mod.interval_path(cache_dir, symbol, "1h")
    content = cache_mod.read_interval_file(path)
    completed, _forming = oc.split_forming(content["bars"], "1h", now_ms)
    return completed, content.get("source")


def build_seed_history(symbol, bars, generated_at, now_ms, count=7):
    """Build a seeded history of predictions for one coin.

    One prediction per completed hour for the last ``count``
    hours, each computed causally — only from bars that had closed
    by that hour's end. Predictions whose target hour has passed
    at ``now_ms`` are carried as revision-1 value rows, retained
    as historical evidence — no retraction rows are published.
    The latest target hour stays live (revision 1 only, same as
    every other row).
    """
    rows = []
    ordered = sorted(bars, key=lambda b: b["t"])
    for bar in ordered[-count:]:
        target_start_ms = oc.bar_end_ms(bar, "1h")
        window = [b for b in ordered
                  if oc.bar_end_ms(b, "1h") <= target_start_ms]
        estimate = est_mod.estimate(window, btc_bars=None, symbol=symbol)
        if estimate is None:
            continue
        rows.append(pred_mod.prediction_to_observation(
            symbol, estimate, target_start_ms, generated_at))
    rows.sort(key=lambda r: (r["eventAt"], r["revision"]))
    # "current" only when the latest target hour has not passed;
    # otherwise "historical" — the value rows are retained, so
    # the seed still demonstrates the full history.
    latest_target = max(oc.instant_ms(r["eventAt"], "eventAt") for r in rows)
    coverage = ("current" if not pred_mod.is_expired(latest_target, now_ms)
                else "historical")
    return pred_mod.build_prediction_file(
        symbol, rows, generated_at, coverage=coverage), coverage


def build_symbol_from_bars(symbol, bars, generated_at, now_ms,
                           btc_bars=None, previous=None):
    """Build one coin's prediction file from completed 1h bars.

    ``bars`` must already be completed bars only (no forming bar).
    Returns ``(content, status)``; see :func:`build_symbol`.
    """
    estimate = est_mod.estimate(bars, btc_bars=btc_bars, symbol=symbol)
    if estimate is None:
        return pred_mod.build_prediction_file(
            symbol, [], generated_at, coverage="absent",
            reason="fewer than 2 completed 1h bars"), "absent"

    last_bar = bars[-1]
    target_start_ms = oc.bar_end_ms(last_bar, "1h")
    rows = []
    # Carry forward previously published predictions whose target
    # hours have now passed (and are still within the 7-day
    # retention window): their revision-1 value rows stay as
    # historical evidence, never retracted. Rows older than the
    # window are dropped, which bounds the file — it is not a
    # retraction of anything still in the file.
    if previous:
        for row in previous.get("observations", []):
            if row.get("revision") != 1 or row.get("value") is None:
                continue
            event_ms = oc.instant_ms(row["eventAt"], "eventAt")
            if event_ms == target_start_ms:
                continue  # superseded by the new estimate below
            if now_ms - pred_mod.target_end_ms(event_ms) <= RETENTION_MS:
                rows.append(dict(row))
    rows.append(pred_mod.prediction_to_observation(
        symbol, estimate, target_start_ms, generated_at))
    # A run that lands after the target hour has already passed
    # publishes the estimate as historical evidence in the same
    # file: the value row is retained (it was never selectable
    # during its hour — its availableAt is after the hour
    # ended), and no retraction row is published.
    coverage = ("historical" if pred_mod.is_expired(target_start_ms, now_ms)
                else "current")
    rows.sort(key=lambda r: (r["eventAt"], r["revision"]))
    return pred_mod.build_prediction_file(
        symbol, rows, generated_at, coverage=coverage), coverage


def build_symbol(symbol, cache_dir, generated_at, now_ms, btc_bars=None,
                 previous=None):
    """Build one coin's prediction file content (or None if absent).

    ``previous`` is the coin's previously published file content
    (if any), used only to carry forward retained predictions
    for past target hours. Returns ``(content, status)`` where
    ``status`` is ``"current"``, ``"historical"`` (the latest
    target hour has passed, but its value is retained) or
    ``"absent"`` with a reason in the content's ``reason`` field.
    """
    try:
        bars, _source = load_completed_1h(cache_dir, symbol, now_ms)
    except FileNotFoundError:
        return pred_mod.build_prediction_file(
            symbol, [], generated_at, coverage="absent",
            reason="no 1h bars: CoinGecko-labelled coins have a 1d base only"), "absent"
    return build_symbol_from_bars(symbol, bars, generated_at, now_ms,
                                  btc_bars=btc_bars, previous=previous)


def build_all(cache_dir, generated_at, now_ms, symbols=None):
    """Build prediction files for the universe (or ``symbols``).

    Returns ``{symbol: (content, status)}``. BTC is built first
    so its bars can serve as the correlation input for the rest;
    a missing BTC file degrades the others to no-correlation
    estimates rather than failing the run.
    """
    symbols = list(symbols) if symbols else coins_mod.names()
    for symbol in symbols:
        if symbol not in coins_mod.BY_SYMBOL:
            raise ValueError(f"unknown symbol: {symbol!r}")
    btc_bars = None
    if "BTC" in symbols:
        try:
            btc_bars, _source = load_completed_1h(cache_dir, "BTC", now_ms)
        except FileNotFoundError:
            btc_bars = None
    out = {}
    for symbol in symbols:
        bars_for_corr = None if symbol == "BTC" else btc_bars
        out[symbol] = build_symbol(symbol, cache_dir, generated_at,
                                    now_ms, btc_bars=bars_for_corr)
    return out


def write_all(out_dir, built, generated_at, universe=None):
    """Write prediction files and provenance sidecars.

    ``built`` is the :func:`build_all` result. Coins with status
    ``"absent"`` get a provenance sidecar but no prediction file —
    the absence is labelled, never an empty file standing in for
    data. Returns ``{symbol: {"keys": [...], "status": ...}}``.
    """
    from . import provenance as prov_mod

    written = {}
    for symbol, (content, status) in built.items():
        keys = []
        if status != "absent":
            path = pred_mod.write_prediction_file(out_dir, symbol, content)
            keys.append(pred_mod.prediction_key(symbol))
        sidecar = prov_mod.build_provenance(
            symbol, content, generated_at=generated_at, universe=universe)
        sidecar_path = Path(out_dir) / "crypto" / "predictions" / symbol / "provenance.json"
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        sidecar_path.write_text(json.dumps(sidecar, indent=2) + "\n", encoding="utf-8")
        keys.append(prov_mod.sidecar_key(symbol))
        written[symbol] = {"keys": keys, "status": status}
    return written
