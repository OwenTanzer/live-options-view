"""Train four opening-chain models and compare frozen, independent paper bots."""

import argparse
import csv
import hashlib
import json
import math
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from sklearn.cluster import KMeans
from sklearn.cross_decomposition import PLSRegression
from sklearn.ensemble import RandomForestRegressor
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "crassus" / "crassus"))
from oi_associator import ARCHITECTURES, SCHEMA, features, forecast

ET = ZoneInfo("America/New_York")
TARGETS = ("10:30", "12:30", "15:30")
COST_BPS = 1.0


def session_record(cache, day):
    folder = cache / day
    with (folder / "chain.csv").open() as f:
        legs = {}
        for row in csv.DictReader(f):
            if row["Expiration"] != day:
                continue
            if row["TradeDate"] != day or row["DTE"] not in ("0", "0.0"):
                raise ValueError("wrong chain date or expiry")
            strike, side = float(row["Strike"]), row["Type"].lower()
            expected = (
                "QQQ"
                + day.replace("-", "")[2:]
                + side[:1].upper()
                + f"{round(strike * 1000):08d}"
            )
            if side not in ("call", "put") or row["OptionSymbol"] != expected:
                raise ValueError("wrong contract identity")
            leg = legs.setdefault(strike, {"strike": strike})
            if side in leg:
                raise ValueError("duplicate side")
            leg[side] = float(row["OpenInterest"]) if row["OpenInterest"] else None
    with (folder / "bars.csv").open() as f:
        bars = {}
        opening_bar = None
        for row in csv.DictReader(f):
            stamp = datetime.fromisoformat(row["datetime"])
            if stamp.tzinfo is None:
                raise ValueError("bar timestamp lacks timezone")
            stamp = stamp.astimezone(ET)
            if stamp.date().isoformat() != day:
                raise ValueError("incorrect bar session")
            clock = stamp.strftime("%H:%M")
            if clock in bars:
                raise ValueError("duplicate bar")
            bars[clock] = float(row["open"])
            if clock == "09:30":
                opening_bar = [float(row[c]) for c in ("low", "close", "high")]
    prices = [bars[t] for t in ("09:30", "09:35", *TARGETS)]
    if not all(math.isfinite(p) and p > 0 for p in prices):
        raise ValueError("invalid bar price")
    spot, entry, *marks = prices
    low, close, high = opening_bar
    if not 0 < low <= min(close, spot) <= max(close, spot) <= high or not math.isfinite(
        high
    ):
        raise ValueError("invalid opening bar range")
    price_features = [
        spot,
        spot - math.floor(spot + 0.5),
        (close / spot - 1) * 10000,
        (high - low) / spot * 10000,
    ]
    ladder = list(legs.values())
    return {
        "session": day,
        "spot": spot,
        "legs": ladder,
        "features": features(spot, ladder),
        "entry": entry,
        "exit": marks[-1],
        "price_features": price_features,
        "path": [(p / entry - 1) * 10000 for p in marks],
    }


def fit(records):
    x = np.array([r["features"] for r in records])
    y = np.array([r["path"] for r in records])
    if len(records) < 12 or len(np.unique(x, axis=0)) < 4:
        raise ValueError(
            "need twelve training sessions and four distinct OI configurations"
        )
    scaler = StandardScaler().fit(x)
    z = scaler.transform(x)
    models = {}
    for name, reg in (
        ("ridge", Ridge(alpha=25.0)),
        ("pls", PLSRegression(n_components=2, scale=False)),
    ):
        reg.fit(z, y)
        intercept = reg.predict(np.zeros((1, z.shape[1])))[0]
        weights = (reg.predict(np.eye(z.shape[1])) - intercept).T
        models[name] = {"intercept": intercept.tolist(), "weights": weights.tolist()}
    forest = RandomForestRegressor(
        n_estimators=32, max_depth=3, min_samples_leaf=4, random_state=17, n_jobs=1
    ).fit(z, y)
    trees = []
    for estimator in forest.estimators_:
        t = estimator.tree_
        trees.append(
            {
                "left": t.children_left.tolist(),
                "right": t.children_right.tolist(),
                "feature": t.feature.tolist(),
                "threshold": t.threshold.tolist(),
                "values": t.value[:, :, 0].tolist(),
            }
        )
    models["forest"] = {"trees": trees}
    groups = KMeans(n_clusters=4, random_state=17, n_init=10).fit(z)
    models["prototype"] = {
        "centers": groups.cluster_centers_.tolist(),
        "paths": [y[groups.labels_ == k].mean(axis=0).tolist() for k in range(4)],
    }
    return {
        "schema": SCHEMA,
        "trained_through": max(r["session"] for r in records),
        "mean": scaler.mean_.tolist(),
        "scale": scaler.scale_.tolist(),
        "models": models,
    }


def evaluate(train, holdout, bundle):
    historical = np.mean([r["path"] for r in train], axis=0)
    price_scale = StandardScaler().fit([r["price_features"] for r in train])
    price_model = Ridge(alpha=25).fit(
        price_scale.transform([r["price_features"] for r in train]),
        [r["path"] for r in train],
    )
    reports = {}
    for name in (
        *ARCHITECTURES,
        "always_long",
        "historical_mean",
        "opening_price_only",
        "cash",
    ):
        ledger, errors = [], []
        cash, peak, drawdown = 10000.0, 10000.0, 0.0
        for r in holdout:
            if name in ARCHITECTURES:
                pred = forecast(bundle, name, r["session"], r["spot"], r["legs"])
            elif name == "opening_price_only":
                pred = price_model.predict(
                    price_scale.transform([r["price_features"]])
                )[0]
            else:
                pred = historical if name == "historical_mean" else None
            take = name == "always_long" or (
                pred is not None and pred[-1] > 2 * COST_BPS
            )
            entry = r["entry"] * (1 + COST_BPS / 10000)
            exit_price = r["exit"] * (1 - COST_BPS / 10000)
            take = bool(take and cash >= entry)
            pnl = exit_price - entry if take else 0.0
            cash += pnl
            peak = max(peak, cash)
            drawdown = max(drawdown, peak - cash)
            if pred is not None:
                errors.append(np.abs(np.asarray(pred) - r["path"]))
            ledger.append(
                {
                    "session": r["session"],
                    "forecast_bps": None if pred is None else list(pred),
                    "actual_path_bps": r["path"],
                    "shares": int(take),
                    "entry": entry if take else None,
                    "exit": exit_price if take else None,
                    "net_pnl": pnl,
                    "cash": cash,
                }
            )
        trades = [d for d in ledger if d["shares"]]
        reports[name] = {
            "sessions": len(holdout),
            "trades": len(trades),
            "wins": sum(d["net_pnl"] > 0 for d in trades),
            "net_pnl": cash - 10000,
            "max_close_to_close_drawdown": drawdown,
            "mae_bps": np.mean(errors, axis=0).tolist() if errors else None,
            "ledger": ledger,
        }
    return reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cache", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--holdout-start", default="2026-08-03", type=date.fromisoformat
    )
    args = parser.parse_args()
    records, exclusions, hashes = [], {}, {}
    dates = json.loads((args.cache / "dates.json").read_text())
    for day in sorted(set(dates)):
        date.fromisoformat(day)
        try:
            record = session_record(args.cache, day)
        except (OSError, ValueError, KeyError) as e:
            exclusions[day] = str(e)
            continue
        records.append(record)
        hashes[day] = {
            k: hashlib.sha256(
                (args.cache / day / (k + ".csv")).read_bytes()
            ).hexdigest()
            for k in ("bars", "chain")
        }
    train = [
        r for r in records if date.fromisoformat(r["session"]) < args.holdout_start
    ]
    holdout = records[len(train) :]
    if not holdout:
        raise ValueError("no held-out sessions")
    bundle = fit(train)
    reports = evaluate(train, holdout, bundle)
    args.output.mkdir(parents=True, exist_ok=False)
    for name, obj in (
        ("bundle", bundle),
        ("results", reports),
        (
            "sources",
            {
                "train": [r["session"] for r in train],
                "holdout": [r["session"] for r in holdout],
                "exclusions": exclusions,
                "sha256": hashes,
            },
        ),
    ):
        (args.output / (name + ".json")).write_text(
            json.dumps(obj, allow_nan=False) + "\n"
        )
    print(f"{len(train)} training, {len(holdout)} holdout, {len(exclusions)} excluded")
    for name, report in reports.items():
        print(
            name,
            report["trades"],
            round(report["net_pnl"], 2),
            round(report["max_close_to_close_drawdown"], 2),
        )


if __name__ == "__main__":
    main()
