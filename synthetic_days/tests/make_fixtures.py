#!/usr/bin/env python3
"""Generate the small, deterministic, checked-in fixture this repo's CI runs
`day_generator.py`/`validate_stats.py`/`backtest_bridge.py` against, instead
of needing real R2 credentials -- flagged in review: CI needs a hermetic
path, and doesn't require exposing R2 secrets to get one.

Not a random smoke fixture: the 5-min bars are generated with a real
stochastic-volatility process (EWMA vol + Student-t innovations), not plain
GBM, specifically so the fixture itself carries the fat tails and
volatility clustering `validate_stats.py`'s acceptance thresholds check
for. A naive iid-Gaussian fixture would make those thresholds fail in CI on
every run regardless of whether the generator code is correct, which would
defeat the point of gating on them.

Re-run this script (deterministic, fixed seed) to regenerate the fixture
after a real schema change; the output is checked into
`tests/fixtures/r2_cache/`.

    python tests/make_fixtures.py
"""

from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ET = ZoneInfo("America/New_York")
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "r2_cache"
RNG = np.random.default_rng(20260810)

N_DAYS = 25
N_DONOR_DAYS = 10
N_INTRADAY_DAYS = 2


def trading_days(start: date, n: int) -> list[date]:
    out = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def simulate_session(spot_open: float, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """78 5-min bars with EWMA stochastic vol + Student-t innovations --
    genuinely fat-tailed and volatility-clustered, not plain GBM."""
    n = 78
    vol = 0.0009  # starting per-bar vol, roughly QQQ-scale
    o, h, l, c, v = [], [], [], [], []
    price = spot_open
    for i in range(n):
        # U-shaped session vol multiplier (higher at open/close).
        session_frac = i / (n - 1)
        u_shape = 1.0 + 0.6 * (abs(session_frac - 0.5) * 2) ** 2
        shock = rng.standard_t(df=4) * vol * u_shape
        bar_o = price
        price = price * (1 + shock)
        bar_c = price
        intrabar = abs(rng.standard_t(df=4)) * vol * u_shape * 0.6
        bar_h = max(bar_o, bar_c) * (1 + intrabar)
        bar_l = min(bar_o, bar_c) * (1 - intrabar)
        o.append(bar_o); h.append(bar_h); l.append(bar_l); c.append(bar_c)
        v.append(int(rng.integers(2000, 60000)))
        # EWMA vol update -- this is what produces clustering: a big move
        # raises near-term vol, a calm patch lets it decay back down.
        vol = 0.85 * vol + 0.15 * (abs(shock) + 0.0004)
    return np.array(o), np.array(h), np.array(l), np.array(c), np.array(v)


def make_5min_bars() -> None:
    days = trading_days(date(2024, 1, 2), N_DAYS)
    t_all, o_all, h_all, l_all, c_all, v_all = [], [], [], [], [], []
    spot = 400.0
    for d in days:
        open_dt = datetime.combine(d, datetime.min.time(), tzinfo=ET).replace(hour=9, minute=30)
        o, h, l, c, v = simulate_session(spot, RNG)
        for i in range(78):
            ts = int((open_dt + timedelta(minutes=5 * i)).astimezone(timezone.utc).timestamp())
            t_all.append(ts); o_all.append(round(float(o[i]), 4)); h_all.append(round(float(h[i]), 4))
            l_all.append(round(float(l[i]), 4)); c_all.append(round(float(c[i]), 4)); v_all.append(int(v[i]))
        spot = float(c[-1])  # next day opens near the prior close

    out_dir = FIXTURE_DIR / "history" / "QQQ" / "5"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "2024.json").write_text(json.dumps({"t": t_all, "o": o_all, "h": h_all, "l": l_all, "c": c_all, "v": v_all}))
    print(f"wrote {len(days)} sessions of 5-min bars")


def make_donor_chain(spot: float, day: date, rng: np.random.Generator) -> dict:
    cols = {k: [] for k in (
        "optionSymbol", "strike", "side", "expiration", "bid", "ask", "mid", "last",
        "volume", "openInterest", "iv", "delta", "gamma", "theta", "vega", "inTheMoney", "underlyingPrice",
    )}
    for k in range(-20, 21):
        strike = round(spot) + k
        for side in ("call", "put"):
            moneyness = strike / spot - 1
            rel_m = -moneyness if side == "call" else moneyness  # OTM positive
            intrinsic = max(0.0, spot - strike) if side == "call" else max(0.0, strike - spot)
            extrinsic = max(0.05, 2.2 * np.exp(-8 * max(rel_m, 0)) * (1 - 0.3 * max(-rel_m, 0) * 20))
            mid = intrinsic + extrinsic + rng.normal(0, 0.03)
            mid = max(0.01, mid)
            spread = mid * rng.uniform(0.02, 0.12)
            bid, ask = round(mid - spread / 2, 2), round(mid + spread / 2, 2)
            oi = int(max(0, rng.normal(4000 * np.exp(-3 * abs(rel_m)), 800)))
            vol = int(max(0, rng.normal(600 * np.exp(-3 * abs(rel_m)), 200)))
            sym = f"QQQ{day.strftime('%y%m%d')}{'C' if side == 'call' else 'P'}{round(strike*1000):08d}"
            cols["optionSymbol"].append(sym); cols["strike"].append(float(strike)); cols["side"].append(side)
            cols["expiration"].append(int(datetime.combine(day, datetime.min.time()).timestamp()))
            cols["bid"].append(bid); cols["ask"].append(ask); cols["mid"].append(round(mid, 2)); cols["last"].append(round(mid, 2))
            cols["volume"].append(vol); cols["openInterest"].append(oi)
            cols["iv"].append(None); cols["delta"].append(None); cols["gamma"].append(None); cols["theta"].append(None); cols["vega"].append(None)
            cols["inTheMoney"].append(intrinsic > 0); cols["underlyingPrice"].append(spot)
    return cols


def make_options_0dte() -> None:
    days = trading_days(date(2024, 2, 1), N_DONOR_DAYS)
    out_dir = FIXTURE_DIR / "history" / "QQQ" / "options_0dte"
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"symbol": "QQQ", "days": {}}
    for i, d in enumerate(days):
        spot = 395.0 + 12.0 * np.sin(i / 2.0) + RNG.normal(0, 3)
        chain = make_donor_chain(float(spot), d, RNG)
        (out_dir / f"{d.isoformat()}.json").write_text(json.dumps(chain))
        manifest["days"][d.isoformat()] = {"contracts": len(chain["strike"])}
    (out_dir / "manifest.json").write_text(json.dumps(manifest))
    print(f"wrote {len(days)} EOD donor chain days")


def make_intraday_days() -> None:
    days = trading_days(date(2024, 3, 4), N_INTRADAY_DAYS)
    intraday_root = FIXTURE_DIR / "intraday"
    intraday_root.mkdir(parents=True, exist_ok=True)
    listing = []
    for d in days:
        spot0 = 400.0 + RNG.normal(0, 5)
        day_dir = intraday_root / d.strftime("%Y%m%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        day_listing = []
        n_snapshots = 14
        for i in range(n_snapshots):
            minutes = i * 30  # every 30 min across the session
            spot = spot0 * (1 + RNG.normal(0, 0.001) * (i + 1))
            decay = max(1.0, 9.0 * np.exp(-minutes / 140.0))  # extrinsic ~9x at open, ~1x by close
            rows = []
            for k in range(-20, 21):
                strike = round(spot) + k
                for side in ("call", "put"):
                    moneyness = strike / spot - 1
                    rel_m = -moneyness if side == "call" else moneyness
                    intrinsic = max(0.0, spot - strike) if side == "call" else max(0.0, strike - spot)
                    base_extrinsic = max(0.03, 0.35 * np.exp(-8 * max(rel_m, 0)))
                    mid = intrinsic + base_extrinsic * decay
                    spread = mid * 0.06
                    bid, ask = round(max(0, mid - spread / 2), 2), round(mid + spread / 2, 2)
                    oi = int(max(0, 3000 * np.exp(-3 * abs(rel_m)) * min(1.0, 0.5 + minutes / 200.0)))
                    vol = int(max(0, 400 * np.exp(-3 * abs(rel_m)) * (minutes / 390.0)))
                    sym = f"QQQ{d.strftime('%y%m%d')}{'C' if side == 'call' else 'P'}{round(strike*1000):08d}"
                    rows.append({
                        "TradeDate": d.isoformat(), "Expiration": d.isoformat(), "Strike": float(strike),
                        "Type": side, "OptionSymbol": sym, "DTE": 0,
                        "OpenInterest": oi, "Volume": vol, "VolDelta": 0,
                        "Bid": bid, "Mid": round(mid, 2), "Ask": ask, "Last": round(mid, 2),
                        "IV": "", "Delta": "", "Gamma": "", "Theta": "", "Vega": "",
                        "UnderlyingPrice": round(float(spot), 2),
                    })
            hhmmss = (datetime.combine(d, datetime.min.time()).replace(hour=9, minute=30) + timedelta(minutes=minutes)).strftime("%H%M%S")
            key_name = f"snapshot_{hhmmss}000000.csv"
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
            (day_dir / key_name).write_text(buf.getvalue())
            day_listing.append(f"intraday/{d.strftime('%Y%m%d')}/{key_name}")
        (day_dir / "_listing.json").write_text(json.dumps(day_listing))
        listing.extend(day_listing)
    (intraday_root / "_listing.json").write_text(json.dumps(listing))
    print(f"wrote {len(days)} intraday collector days")


def main() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    make_5min_bars()
    make_options_0dte()
    make_intraday_days()
    print(f"\nFixture written to {FIXTURE_DIR}")


if __name__ == "__main__":
    main()
