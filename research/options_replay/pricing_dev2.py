"""Development candidate s3c-dev2 (S3C_PROTOCOL.md Amendment 1). It is not frozen and not validated.

It subclasses the frozen combined model (`pricing.SymbolModel`, unchanged) and applies exactly the four
declared corrections, one per component:

- clock: implied clock. Bucket weights come from the development median decay of ATM straddle-implied
  remaining variance (they are passed in as `weights`, so the clock code is unchanged).
- volatility response: realized-vol feedback, sigma_t * (k * RV60(t) / sigma0) ** eta, where RV60 is the
  trailing 60-minute realized vol of the path, passed in by the caller (a generator knows its own path).
- expiration (a modelling assumption, not a settlement rule): the post-close variance decays linearly to
  a fitted horizon H (seconds after midnight ET) instead of the assumed 17:30.
- spreads: separate h0 + h1*mid for the ATM strike (nearest spot) and for other strikes, per window.

Parameters come from `calibration/s3c_params_dev2.json`.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pricing as PR

DEV2_PARAMS_PATH = Path(__file__).resolve().parent / "calibration" / "s3c_params_dev2.json"
CANDIDATE = "s3c-dev2"


class Dev2Model(PR.SymbolModel):
    def __init__(self, p: dict):
        super().__init__(p)
        self.eta = float(p.get("eta", 0.0))
        self.horizon = float(p.get("post_close_horizon_s", PR.DECISION))
        self.spread_atm = p.get("spread_atm", {})
        self.spread_otm = p.get("spread_otm", {})
        self.path: dict[int, float] = {}  # {time of day: spot} of the current day/path (set by the caller)

    def rv60(self, t: float) -> float | None:
        """Trailing 60-minute realized vol of the path up to t (5-minute steps, model clock)."""
        if not self.path:
            return None
        ss = cov = 0.0
        t0 = int(t) - 3600
        for u in range(t0 - t0 % 300, int(t) - 299, 300):
            a, b = self.path.get(u), self.path.get(u + 300)
            if a and b and u + 300 <= t and u >= PR.OPEN:
                ss += math.log(b / a) ** 2
                cov += self.covered(u, u + 300)
        return math.sqrt(ss / cov * PR.YEAR_DAYS) if cov > 1e-6 else None

    def tau(self, t: float) -> float:
        if t < PR.CLOSE:
            return super().tau(t)
        if t >= self.horizon:
            return 0.0
        return self.tail * (self.horizon - t) / (self.horizon - PR.CLOSE) / PR.YEAR_DAYS

    def sigma_at(self, sigma0, S, S_ref, t=None):
        s = super().sigma_at(sigma0, S, S_ref, t)
        rv = self.rv60(t) if (self.eta and t is not None) else None
        if rv and self.k and sigma0 > 0:
            s *= (self.k * rv / sigma0) ** self.eta
        return s

    def quote(self, S, K, side, t, sigma0, S_ref):
        m = self.mid(S, K, side, t, sigma0, S_ref)
        table = self.spread_atm if abs(K - S) <= 0.5 else self.spread_otm
        name = next((n for n, a, b in PR.SPREAD_WINDOWS if a <= t < b), "endpoint")
        h0, h1 = table.get(name, self.spread.get(name, (0.005, 0.0)))
        h = max(0.005, h0 + h1 * m)
        bid = max(0.0, math.floor(round((m - h) * 100, 6)) / 100)
        ask = max(0.01, math.ceil(round((m + h) * 100, 6)) / 100)
        if ask <= bid:
            ask = round(bid + 0.01, 2)
        return round(bid, 2), round(ask, 2), m


def load_dev2(path: Path | str = DEV2_PARAMS_PATH) -> dict:
    return json.loads(Path(path).read_text())
