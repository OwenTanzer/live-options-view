"""Combined 0DTE quote model (OA-237 S3c; generator sim-v2).

One volatility surface per symbol prices calls and puts. With r = 0 that makes model mids
satisfy C - P = S - K exactly. The parts:

- variance clock: business-time weights per 5-minute bucket 09:30-16:00, plus a post-close
  variance term. Expiring SPY/QQQ options trade until 16:15 ET; automatic exercise is decided
  from the underlying's 16:00 close; contrary exercise instructions are possible after the close
  (exchange cutoff 17:30 ET). The post-close term decays linearly from 16:00 to 17:30 and is
  fitted on development quotes from 15:55-16:14;
- volatility state: sigma_t = sigma0 * (S_t / S_ref) ** beta * exp(gamma(t)): an intraday
  response to the underlying plus a time-of-day drift gamma (per 30-minute bucket, 0 at 09:45);
- skew/convexity: sigma(K) = sigma_t * (1 + a g + b g^2), g = z_c tanh(z / z_c),
  z = ln(K/S) / (sigma_t sqrt(tau)). The smooth saturation at the fitted range z_c = 3 means the
  smile is never extrapolated. An unbounded quadratic wing makes far out-of-the-money prices rise
  with distance (development revision D1);
- spreads: symbol-specific half-spread h = h0 + h1 * mid per time window. Bid and ask are rounded
  outward to the $0.01 grid; bid >= 0, ask >= $0.01, ask > bid.

Every parameter comes from `calibration/s3c_params.json`, fitted on development days only
(`calibration/evaluate_s3c.py --fit`). This module never fits anything.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

OPEN = 9 * 3600 + 30 * 60
CLOSE = 16 * 3600
OPTIONS_CLOSE = 16 * 3600 + 15 * 60
DECISION = 17 * 3600 + 30 * 60
BUCKETS = 78
YEAR_DAYS = 252.0
SPREAD_WINDOWS = (("open", OPEN, 10 * 3600 + 30 * 60), ("midday", 10 * 3600 + 30 * 60, 14 * 3600 + 30 * 60),
                  ("close", 14 * 3600 + 30 * 60, 15 * 3600 + 50 * 60), ("endpoint", 15 * 3600 + 50 * 60, OPTIONS_CLOSE + 1))
PARAMS_PATH = Path(__file__).resolve().parent / "calibration" / "s3c_params.json"


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs(S: float, K: float, sigma: float, tau: float, side: str) -> float:
    intrinsic = max(0.0, S - K) if side == "C" else max(0.0, K - S)
    if tau <= 0 or sigma <= 0:
        return intrinsic
    v = sigma * math.sqrt(tau)
    d1 = (math.log(S / K) + 0.5 * v * v) / v
    d2 = d1 - v
    if side == "C":
        return S * _ncdf(d1) - K * _ncdf(d2)
    return K * _ncdf(-d2) - S * _ncdf(-d1)


class SymbolModel:
    def __init__(self, p: dict):
        w = p["weights"]
        tot = sum(w)
        self.w = [x / tot for x in w]
        self.tail = float(p.get("tail", 0.0))
        self.beta = float(p.get("beta", 0.0))
        self.a = float(p.get("skew_a", 0.0))
        self.b = float(p.get("skew_b", 0.0))
        self.k = p.get("k")
        self.spread = p.get("spread", {})
        self.gamma = p.get("gamma", {})          # {"HH:MM" of 30-min bucket start: log drift}
        self.zc = float(p.get("skew_z_cap", 3.0))

    # -- clock ---------------------------------------------------------------
    def tau(self, t: float) -> float:
        """Remaining variance time in years at time of day t (seconds, ET)."""
        if t < CLOSE:
            x = (max(t, OPEN) - OPEN) / 300.0
            b = int(x)
            rem = (1.0 - (x - b)) * self.w[b] + sum(self.w[b + 1:])
            return (rem + self.tail) / YEAR_DAYS
        if t >= DECISION:
            return 0.0
        return self.tail * (DECISION - t) / (DECISION - CLOSE) / YEAR_DAYS

    def covered(self, t0: float, t1: float) -> float:
        return (self.tau(t0) - self.tau(t1)) * YEAR_DAYS

    # -- volatility surface --------------------------------------------------
    def drift(self, t: float) -> float:
        if not self.gamma:
            return 0.0
        b = int(min(max(t, OPEN), CLOSE - 1) // 1800 * 1800)
        return float(self.gamma.get(f"{b // 3600:02d}:{b % 3600 // 60:02d}", 0.0))

    def sigma_at(self, sigma0: float, S: float, S_ref: float, t: float | None = None) -> float:
        s = sigma0 * (S / S_ref) ** self.beta if S_ref else sigma0
        return s * math.exp(self.drift(t)) if t is not None else s

    def sigma_k(self, sig: float, S: float, K: float, tau: float) -> float:
        if tau <= 0 or sig <= 0:
            return sig
        z = math.log(K / S) / (sig * math.sqrt(tau))
        g = self.zc * math.tanh(z / self.zc)
        return sig * max(0.5, 1.0 + self.a * g + self.b * g * g)

    def mid(self, S: float, K: float, side: str, t: float, sigma0: float, S_ref: float) -> float:
        tau = self.tau(t)
        sig = self.sigma_at(sigma0, S, S_ref, t)
        return bs(S, K, self.sigma_k(sig, S, K, tau), tau, side)

    # -- quotes --------------------------------------------------------------
    def half_spread(self, mid: float, t: float) -> float:
        for name, a, b in SPREAD_WINDOWS:
            if a <= t < b:
                h0, h1 = self.spread.get(name, (0.005, 0.0))
                return max(0.005, h0 + h1 * mid)
        h0, h1 = self.spread.get("endpoint", (0.005, 0.0))
        return max(0.005, h0 + h1 * mid)

    def quote(self, S: float, K: float, side: str, t: float, sigma0: float, S_ref: float) -> tuple[float, float, float]:
        m = self.mid(S, K, side, t, sigma0, S_ref)
        h = self.half_spread(m, t)
        bid = max(0.0, math.floor(round((m - h) * 100, 6)) / 100)
        ask = max(0.01, math.ceil(round((m + h) * 100, 6)) / 100)
        if ask <= bid:
            ask = round(bid + 0.01, 2)
        return round(bid, 2), round(ask, 2), m


def load_params(path: Path | str = PARAMS_PATH) -> dict:
    return json.loads(Path(path).read_text())


def model_for(symbol: str, params: dict | None = None) -> SymbolModel:
    params = params or load_params()
    return SymbolModel(params["symbols"][symbol])
