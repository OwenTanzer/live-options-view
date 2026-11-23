import copy
import datetime as dt
import json
import unittest
from pathlib import Path

import numpy as np
from sklearn.cluster import KMeans
from sklearn.cross_decomposition import PLSRegression
from sklearn.ensemble import RandomForestRegressor
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler

from research.oi_associator.train import (
    ARCHITECTURES,
    evaluate,
    features,
    fit,
    forecast,
)


class AssociatorTest(unittest.TestCase):
    @staticmethod
    def record(i):
        spot = 450.25
        legs = [
            {
                "strike": 450 + k,
                "call": ((i + 2) * (k + 9)) ** 2 % 331,
                "put": (i * i + 3 * (k + 4)) % 199,
            }
            for k in range(-3, 4)
        ]
        return {
            "session": str(dt.date(2025, 1, 1) + dt.timedelta(days=i)),
            "spot": spot,
            "legs": legs,
            "features": features(spot, legs),
            "path": [i * 1.0, i % 5 - 2.0, i % 11 - 4.0],
            "entry": 450,
            "exit": 451,
            "price_features": [0.25, i, 2.0],
        }

    def test_exported_models_match_fitted_estimators(self):
        train = [self.record(i) for i in range(28)]
        r = self.record(40)
        bundle = fit(train)
        x = [t["features"] for t in train]
        y = np.array([t["path"] for t in train])
        scale = StandardScaler().fit(x)
        z, point = scale.transform(x), scale.transform([r["features"]])
        cluster = KMeans(n_clusters=4, random_state=17, n_init=10).fit(z)
        expected = {
            "ridge": Ridge(alpha=25).fit(z, y).predict(point)[0],
            "forest": RandomForestRegressor(
                n_estimators=32, max_depth=3, min_samples_leaf=4, random_state=17
            )
            .fit(z, y)
            .predict(point)[0],
            "prototype": y[cluster.labels_ == cluster.predict(point)[0]].mean(axis=0),
            "pls": PLSRegression(n_components=2, scale=False)
            .fit(z, y)
            .predict(point)[0],
        }
        for name in ARCHITECTURES:
            actual = forecast(bundle, name, r["session"], r["spot"], r["legs"])
            np.testing.assert_allclose(actual, expected[name], atol=1e-9)
        with self.assertRaises(ValueError):
            forecast(bundle, "ridge", bundle["trained_through"], r["spot"], r["legs"])

    def test_missing_zero_duplicate_and_ledger(self):
        records = [self.record(i) for i in range(25)]
        r = records[-1]
        with self.assertRaises(ValueError):
            features(r["spot"], r["legs"][1:])
        with self.assertRaises(ValueError):
            features(r["spot"], r["legs"] + [r["legs"][0]])
        bad = copy.deepcopy(r["legs"])
        bad[0]["call"] = None
        with self.assertRaises(ValueError):
            features(r["spot"], bad)
        self.assertEqual(len(features(r["spot"], r["legs"])), 15)
        reports = evaluate(records[:-1], [r], fit(records[:-1]))
        self.assertAlmostEqual(
            reports["always_long"]["net_pnl"], 451 * 0.9999 - 450 * 1.0001
        )
        self.assertEqual(reports["cash"]["net_pnl"], 0)
        for name in ARCHITECTURES:
            self.assertEqual(len(reports[name]["ledger"]), 1)

    def test_shipped_bundle_has_all_deployable_forecasts(self):
        path = (
            Path(__file__).resolve().parents[2]
            / "crassus/crassus/oi_associator_v1.json"
        )
        b = json.loads(path.read_text())
        r = self.record(1)
        for name in ARCHITECTURES:
            self.assertTrue(
                np.isfinite(forecast(b, name, "2026-11-23", r["spot"], r["legs"])).all()
            )
