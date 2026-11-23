"""Simulation mode for the options trainer (OA-237 S3a): a generated, tradable day.

Mechanism (reused, not reinvented): `research/synthetic_days`
- price path: `price_path.bootstrap_day` -- moving block bootstrap of real QQQ
  5-minute log returns (30-minute blocks drawn from the same time-of-day
  position across real sessions), upsampled to 60 s with a Brownian bridge
  scaled to each bar's real high-low range (`upsample_to_seconds`);
- option quotes: `chain_synth.synth_snapshot_rows` -- one real end-of-day 0DTE
  chain is drawn as a donor; each contract is intrinsic value plus the
  donor's extrinsic value at the nearest signed moneyness, scaled by a
  time-of-day x moneyness decay grid measured from real collector intraday
  snapshots; bid/ask = mid -/+ half the donor's relative spread at that
  moneyness, scaled by a time-of-day spread ramp.

Calibration source (R2 `qqq-options-chain-data`, read-only): `history/QQQ/5/{year}.json`
(5-minute bars), `history/QQQ/options_0dte/{date}.json` (EOD donor chains, up
to 80 evenly spaced days) and `intraday/{YYYYMMDD}/snapshot_*.csv` (up to 15
days x 30 snapshots for the intraday grid). Every input object actually used
is listed with its SHA-256 in an inputs manifest; the manifest digest, the
seed and GENERATOR_VERSION fully determine a generated day.

PROVISIONAL PRICING (S3a): quotes are model output, not recorded market
quotes. Empirical time-decay calibration is S3b; combined pricing/spread
checks (incl. put-call parity, which this construction does not enforce)
are S3c. A generated day is never presented as historical Replay.
"""

from __future__ import annotations

import hashlib
import json
import os
import pickle
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
SYN = HERE.parent / "synthetic_days"
sys.path.insert(0, str(SYN))
sys.path.insert(0, str(HERE))

import archive as A  # noqa: E402

GENERATOR_VERSION = "sim-v1"          # S3a donor pricing (kept regenerable for existing seeds)
SIM_V2 = "sim-v2"                     # S3c combined model (pricing.py + calibration/s3c_params.json)
SUPPORTED_VERSIONS = (GENERATOR_VERSION, SIM_V2)
DEFAULT_VERSION = SIM_V2
LIMITATIONS_V2 = [
    "Quotes come from the S3c combined model fitted on development days only; no held-out validation "
    "exists yet for this provisional practice model.",
    "On development days it still misses absolute level error (about $9-12/contract at midday/close), "
    "30-minute hold profit (about $4-6) and opening decay; see calibration/S3C_RESULTS.md.",
    "The volatility level comes from the generated path's realized volatility times a fitted ratio; a real day's "
    "implied level is not knowable from a path, so day-to-day level dispersion is not reproduced.",
    f"The generated session ends at 16:00; real expiring {A.SYMBOL} options trade until 16:15 (SEC Rel. 34-105153). "
    "The post-close variance term (decaying to 17:30, the FINRA 2360 exercise-decision deadline) is a modelling "
    "assumption fitted to development quotes, not a settlement rule.",
    "The path is resampled from real 5-minute returns; intrabar moves are a Brownian bridge, not ticks.",
]
# Generated sessions are labelled with a fixed, obviously synthetic calendar date that only
# serves the generator's timestamp arithmetic (an EST weekday); it is never shown as a date.
LABEL_DATE = date(2000, 1, 3)
OPEN_S, CLOSE_S = 9 * 3600 + 30 * 60, 16 * 3600
BLOCK_SIZE = 6
STRIKE_WINDOW = 33
MAX_DONOR_DAYS = 80
MAX_INTRADAY_DAYS = 15
INTRADAY_SAMPLES_PER_DAY = 30
YEARS = (2023, 2024, 2025, 2026)
MECHANISM = ("moving block bootstrap of real QQQ 5-minute returns (30-minute blocks, same time-of-day "
             "position) upsampled to 60 s; option quotes = intrinsic + donor EOD 0DTE extrinsic at the "
             "nearest moneyness, scaled by a time-of-day x moneyness decay grid and a spread ramp measured "
             "from real collector intraday snapshots (research/synthetic_days)")
LIMITATIONS = [
    "Quotes are provisional model output, not recorded market quotes.",
    "One donor EOD chain sets the whole day's volatility/skew shape; it does not react to the path.",
    "Time decay uses a 15-minute x 5-moneyness-bucket grid from up to 15 collector days (S3b recalibrates).",
    "Calls and puts are priced independently, so put-call parity is not enforced (checked in S3c).",
    "Spreads follow the donor's relative spread by moneyness and a time-of-day ramp; no size or liquidity model.",
    "The path is resampled from real 5-minute returns; intrabar moves are a Brownian bridge, not real ticks.",
    "No holidays, half-days or news shocks are modelled; every generated session is a full 9:30-16:00 day.",
]


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


PIN_PATH = HERE / "sim_inputs_pin.json"


class PinError(RuntimeError):
    """A fresh install could not reproduce the frozen simulation inputs; nothing was written."""


def load_pin(path: Path = PIN_PATH) -> dict:
    if not path.exists():
        raise PinError(f"pinned input inventory {path.name} is missing; refusing to build inputs from the live archive")
    return json.loads(path.read_text())


def pinned_source(pin: dict, cache_dir: Path, s3=None):
    """An R2Source restricted to the pinned inventory.

    Only pinned keys can be fetched; every listing method returns the pinned days/keys instead of
    listing R2, so days collected after the freeze (including the reserved S3c forward sessions)
    are never selected or read. Each fetched object must match its pinned SHA-256, except the
    year files of 5-minute bars, which keep growing: those are decoded and cut at the pinned last
    bar before anything is built from them, and must then match the pinned count and range.
    """
    from r2_sources import R2Source

    objects = pin["manifest"]["r2_objects"]
    bar_keys = {b["key"] for b in pin["bars"].values()}

    class PinnedR2Source(R2Source):
        def _get_bytes(self, key: str) -> bytes | None:
            if key not in objects:
                raise PinError(f"{key} is not in the pinned input inventory")
            raw = super()._get_bytes(key)
            if raw is None:
                raise PinError(f"pinned input {key} could not be read (R2 read credentials are needed the first time)")
            if _sha(raw) != objects[key] and key not in bar_keys:
                self._cache_path(key).unlink(missing_ok=True)
                raise PinError(f"pinned input {key} changed in the archive (sha256 differs); refusing to rebuild")
            return raw

        def _list(self, prefix: str) -> list[str]:
            raise PinError(f"listing {prefix} is not allowed; inputs come from the pinned inventory only")

        def five_min_bars(self, symbol: str, years: list[int]) -> dict:
            out = {}
            for year in years:
                spec = pin["bars"].get(str(year))
                if spec is None or spec["key"] != f"history/{symbol}/5/{year}.json":
                    raise PinError(f"5-minute bars {symbol} {year} are not pinned")
                data = json.loads(self._get_bytes(spec["key"]))
                keep = [i for i, t in enumerate(data["t"]) if t <= spec["last_t"]]
                data = {k: [v[i] for i in keep] for k, v in data.items()}
                if len(data["t"]) != spec["n"] or (data["t"] and data["t"][0] != spec["first_t"]):
                    raise PinError(f"5-minute bars {year} up to the pinned last bar no longer match the pin")
                out[year] = data
            return out

        def all_options_0dte_days(self, symbol: str) -> list[date]:
            return [date.fromisoformat(d) for d in pin["donor_days"]]

        def intraday_days_available(self) -> list[str]:
            return sorted(pin["intraday_keys"])

        def intraday_snapshots(self, yyyymmdd: str) -> list[str]:
            return sorted(pin["intraday_keys"].get(yyyymmdd, []))

    return PinnedR2Source(cache_dir=cache_dir, s3=s3)


def build_inputs(src) -> tuple:
    """(sessions, donors, curve) from a source, using the sim-v1 settings."""
    from price_path import split_into_sessions
    import day_generator as G
    bars = src.five_min_bars("QQQ", list(YEARS))
    if not bars:
        raise PinError("no 5-minute history available")
    sessions = split_into_sessions(bars)
    donors = G.load_donors(src, "QQQ", max_days=MAX_DONOR_DAYS)
    curve = G.load_intraday_curve(src, max_days=MAX_INTRADAY_DAYS, samples_per_day=INTRADAY_SAMPLES_PER_DAY)
    if not donors or curve is None:
        raise PinError("missing donor chains or intraday calibration")
    return sessions, donors, curve


def rebuild_from_pin(cache: Path, pin: dict | None = None, s3=None, builder=build_inputs) -> tuple:
    """Rebuild the frozen inputs from the pinned inventory, or stop. Returns (inputs, manifest)."""
    pin = pin or load_pin()
    os.environ["SYNTHETIC_DAYS_CACHE"] = str(cache / "r2")
    inputs = builder(pinned_source(pin, cache / "r2", s3=s3))
    blob = pickle.dumps(inputs, protocol=4)
    manifest = pin["manifest"]
    if _sha(blob) != manifest["frozen_sha256"]:
        raise PinError("fresh rebuild did not reproduce the frozen simulation inputs (frozen_sha256 "
                       f"{manifest['frozen_sha256'][:16]}..., got {_sha(blob)[:16]}...); nothing was written")
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "frozen_inputs.pkl").write_bytes(blob)
    (cache / "inputs_manifest.json").write_text(json.dumps(manifest, indent=1))
    return inputs, manifest


class Simulator:
    """Loads the calibration inputs once (cached, read-only R2) and generates days by seed."""

    def __init__(self, cache_dir: Path | str):
        self.cache = Path(cache_dir) / "sim_inputs"
        self.cache.mkdir(parents=True, exist_ok=True)
        self._inputs = None
        self.manifest = None

    def _load_inputs(self):
        if self._inputs is not None:
            return self._inputs
        env_file = os.environ.get("OPTIONS_REPLAY_ENV_FILE")
        if env_file:
            A._load_env_file(Path(env_file))
        if A.SYMBOL == "SPY":
            from price_path import DayBars
            import numpy as np
            blob = (self.cache / "spy_donors.json").read_bytes()
            sessions = [DayBars(date.fromisoformat(d["day"]), *(np.asarray(d[k]) for k in ("t","o","h","l","c","v"))) for d in json.loads(blob)]
            self.manifest = {"inputs_digest": _sha(blob), "historical_sessions": len(sessions), "r2_objects": {}}
            self._inputs = sessions, [], None
            return self._inputs
        frozen = self.cache / "frozen_inputs.pkl"
        man_path = self.cache / "inputs_manifest.json"
        if frozen.exists() and man_path.exists():
            self.manifest = json.loads(man_path.read_text())
            blob = frozen.read_bytes()
            if _sha(blob) != self.manifest["frozen_sha256"]:
                raise RuntimeError("frozen simulation inputs do not match their manifest")
            if PIN_PATH.exists() and self.manifest["inputs_digest"] != load_pin()["manifest"]["inputs_digest"]:
                raise PinError("cached simulation inputs are not the pinned frozen inputs (inputs_digest differs)")
            self._inputs = pickle.loads(blob)
            return self._inputs
        # Fresh cache: rebuild only from the pinned development inventory (never from a listing of
        # the evolving archive), and accept the result only if it is byte-identical to the frozen inputs.
        self._inputs, self.manifest = rebuild_from_pin(self.cache)
        return self._inputs

    def inputs_digest(self) -> str:
        self._load_inputs()
        return self.manifest["inputs_digest"]

    def generate(self, seed: int, version: str = GENERATOR_VERSION) -> dict:
        """A Replay-format pack for one generated session. Same seed + inputs + version -> same pack."""
        if version == SIM_V2:
            return self._generate_v2(seed)
        if version != GENERATOR_VERSION:
            raise ValueError(f"unknown generator version {version}")
        import numpy as np
        import day_generator as G
        sessions, donors, curve = self._load_inputs()
        rng = np.random.default_rng(int(seed))
        day = G.generate_day(sessions, donors, curve, rng, LABEL_DATE, "block", BLOCK_SIZE, STRIKE_WINDOW)
        snaps = []
        for s in day["snapshots"]:
            hh, mm = (int(x) for x in s["snapshot_time"].split()[0].split(":"))
            t = hh * 3600 + mm * 60
            if not OPEN_S <= t <= CLOSE_S:
                continue
            q = {}
            for r in s["rows"]:
                k = ("C" if r["Type"] == "call" else "P") + A._fmt_strike(float(r["Strike"]))
                q[k] = [r["Bid"], r["Ask"]]
            snaps.append({"t": t, "key": f"sim/{GENERATOR_VERSION}/{int(seed)}/{t // 3600:02d}{t % 3600 // 60:02d}",
                          "spot": round(float(s["underlying_price"]), 4), "q": q})
        A.assess(snaps)
        # Simulation gate: the generated spot is exact and quotes are derived from it, so the
        # recorded-data checks (gap/frozen/parity) do not apply. Keep the parity residual as a
        # reported pricing diagnostic (S3c) instead of disabling quotes on it.
        residuals = []
        for sn in snaps:
            if sn["parity_spot"] is not None:
                residuals.append(abs(sn["parity_spot"] - sn["spot"]))
            sn["parity_reason"] = sn["reason"]
            sn["ok"], sn["reason"] = (sn["spot"] is not None and bool(sn["q"])), None
            sn["content_t"] = sn["t"]
        body = json.dumps([[s["t"], s["spot"], s["q"]] for s in snaps], sort_keys=True,
                          separators=(",", ":")).encode()
        residuals.sort()
        sim = {
            "seed": int(seed), "generator_version": GENERATOR_VERSION,
            "inputs_digest": self.manifest["inputs_digest"], "pack_sha256": _sha(body),
            "donor_day": day["donor_day"], "mechanism": MECHANISM, "limitations": LIMITATIONS,
            "calibration": {"historical_sessions": self.manifest["historical_sessions"],
                            "donor_days": len(self.manifest["donor_days"]),
                            "r2_objects": len(self.manifest["r2_objects"])},
            "parity_residual_usd": {"median": residuals[len(residuals) // 2] if residuals else None,
                                    "p95": residuals[int(len(residuals) * 0.95)] if residuals else None,
                                    "max": residuals[-1] if residuals else None},
        }
        return {"version": A.PACK_VERSION, "mode": "simulation",
                "day": f"SIM:{GENERATOR_VERSION}:{int(seed)}:{self.manifest['inputs_digest'][:12]}",
                "open_s": OPEN_S, "close_s": CLOSE_S,
                "coverage_all": {"ratio": 1.0}, "coverage_usable": A.playable_coverage(snaps, OPEN_S, CLOSE_S),
                "snapshots": snaps, "sim": sim}

    def _generate_v2(self, seed: int) -> dict:
        """sim-v2: the sim-v1 path machinery, priced by the S3c combined model."""
        import numpy as np
        import pricing as PR
        from price_path import bootstrap_day, upsample_to_seconds
        sessions, _, _ = self._load_inputs()
        params_bytes = PR.PARAMS_PATH.read_bytes()
        params = json.loads(params_bytes)
        model = PR.SymbolModel(params["symbols"][A.SYMBOL])
        rng = np.random.default_rng(int(seed))
        volume = []
        closes, ranges = bootstrap_day(sessions, rng, mode="block", block_size=BLOCK_SIZE, sampled_volume=volume)
        timestamps, path = upsample_to_seconds(LABEL_DATE, closes, ranges, rng, step_s=60)
        series = []
        for ts, spot in zip(timestamps, path):
            t = ts.hour * 3600 + ts.minute * 60
            if OPEN_S <= t <= CLOSE_S:
                series.append((t, round(float(spot), 4)))
        by_t = dict(series)
        ss = cov = 0.0
        for t, spot in series:                       # realized vol of the generated path, model clock
            nxt = by_t.get(t + 300)
            if nxt and t % 300 == 0:
                ss += float(np.log(nxt / spot)) ** 2
                cov += model.covered(t, t + 300)
        rv = (ss / cov * PR.YEAR_DAYS) ** 0.5 if cov > 0 else None
        sigma0 = model.k * rv
        s_ref = by_t.get(9 * 3600 + 45 * 60, series[0][1])
        snaps = []
        for t, spot in series:
            q = {}
            centre = round(spot)
            for K in range(centre - STRIKE_WINDOW, centre + STRIKE_WINDOW + 1):
                for side in ("C", "P"):
                    b, a, _ = model.quote(spot, float(K), side, t, sigma0, s_ref)
                    q[f"{side}{K}"] = [b, a]
            snaps.append({"t": t, "key": f"sim/{SIM_V2}/{int(seed)}/{t // 3600:02d}{t % 3600 // 60:02d}",
                          "spot": spot, "q": q, "ok": True, "reason": None, "content_t": t,
                          "parity_spot": None})
        # Show each donor volume only after its five-minute interval is complete.
        for snapshot in snaps:
            elapsed = snapshot["t"] - OPEN_S
            if elapsed > 0 and elapsed % 300 == 0:
                snapshot["volume"] = volume[elapsed // 300 - 1]
        digest = _sha((self.manifest["inputs_digest"] + _sha(params_bytes)).encode())
        body = json.dumps([[x["t"], x["spot"], x["q"]] for x in snaps], sort_keys=True, separators=(",", ":")).encode()
        sim = {"seed": int(seed), "generator_version": SIM_V2, "inputs_digest": digest,
               "path_inputs_digest": self.manifest["inputs_digest"], "pricing_params_sha256": _sha(params_bytes),
               "pack_sha256": _sha(body), "donor_day": None, "sigma0": round(sigma0, 6), "path_realized_vol": round(rv, 6),
               "mechanism": (f"path: moving block bootstrap of {A.SYMBOL} five-minute returns; quotes: combined "
                             "model -- business-time variance clock with post-close term, sigma0 = k x the path's realized "
                             "vol, spot response beta and time-of-day drift, saturated skew surface shared by calls and "
                             "puts (parity exact), symbol-specific spreads rounded to $0.01"),
               "limitations": LIMITATIONS_V2,
               "calibration": {"historical_sessions": self.manifest["historical_sessions"],
                               "donor_days": 0, "r2_objects": len(self.manifest["r2_objects"]),
                               "pricing_development_days": params["symbols"][A.SYMBOL]["development_days"]},
               "parity_residual_usd": {"median": 0.0, "p95": 0.0, "max": 0.0}}
        return {"version": A.PACK_VERSION, "mode": "simulation",
                "volume_unit": "resampled shares" if A.SYMBOL == "QQQ" else "resampled option contracts",
                "day": f"SIM:{SIM_V2}:{int(seed)}:{digest[:12]}",
                "open_s": OPEN_S, "close_s": CLOSE_S, "coverage_all": {"ratio": 1.0},
                "coverage_usable": A.playable_coverage(snaps, OPEN_S, CLOSE_S), "snapshots": snaps, "sim": sim}

    def raw_quote(self, seed: int, key: str, contract: str, version: str = GENERATOR_VERSION) -> dict | None:
        """Independent regeneration of one quote, for reconciliation of simulated fills."""
        pack = self.generate(seed, version)
        for sn in pack["snapshots"]:
            if sn["key"] == key:
                b, a = sn["q"].get(contract, [None, None])
                model_inputs = {}
                if version == SIM_V2:
                    import pricing as PR
                    model = PR.model_for(A.SYMBOL)
                    reference = next((s["spot"] for s in pack["snapshots"] if s["t"] == OPEN_S + 900),
                                     pack["snapshots"][0]["spot"])
                    tau = model.tau(sn["t"])
                    sigma = model.sigma_at(pack["sim"]["sigma0"], sn["spot"], reference, sn["t"])
                    model_inputs = {"model_tau": tau, "model_iv": model.sigma_k(sigma, sn["spot"], float(contract[1:]), tau)}
                return {**model_inputs, "key": key, "OptionSymbol": f"SIM {contract}", "Bid": b, "Ask": a,
                        "UnderlyingPrice": sn["spot"], "regenerated": True,
                        "pack_sha256": pack["sim"]["pack_sha256"]}
        return None


def parse_sim_label(day: str) -> dict | None:
    if not isinstance(day, str) or not day.startswith("SIM:"):
        return None
    _, ver, seed, digest = day.split(":")
    return {"generator_version": ver, "seed": int(seed), "inputs_digest_prefix": digest}


def compare_with_recorded(sim: Simulator, cache: Path, seeds=range(1, 21), version: str = GENERATOR_VERSION) -> list[dict]:
    """ATM straddle mid (% of spot) and half-spread, generated vs recorded Replay packs."""
    import bisect
    import gzip
    import statistics as st
    times = {"10:00": 36000, "12:00": 43200, "14:00": 50400, "15:30": 55800}

    def straddle(pack, t):
        ts = [x["t"] for x in pack["snapshots"]]
        i = bisect.bisect_right(ts, t) - 1
        if i < 0 or not pack["snapshots"][i]["ok"]:
            return None
        sn = pack["snapshots"][i]
        k = A._fmt_strike(min({float(c[1:]) for c in sn["q"]}, key=lambda x: abs(x - sn["spot"])))
        c, p = sn["q"].get("C" + k), sn["q"].get("P" + k)
        if not c or not p or None in c + p:
            return None
        return (sum(c) + sum(p)) / 2 / sn["spot"] * 100, (c[1] - c[0] + p[1] - p[0]) / 2
    rec = {k: [] for k in times}
    gen = {k: [] for k in times}
    for f in sorted((cache / "packs").glob("*.json.gz")):
        pk = json.loads(gzip.decompress(f.read_bytes()))
        for k, t in times.items():
            r = straddle(pk, t)
            if r:
                rec[k].append(r)
    for seed in seeds:
        pk = sim.generate(seed, version)
        for k, t in times.items():
            r = straddle(pk, t)
            if r:
                gen[k].append(r)
    out = []
    for k in times:
        if rec[k] and gen[k]:
            rm, gm = st.median(x[0] for x in rec[k]), st.median(x[0] for x in gen[k])
            out.append({"time": k, "recorded_pct": round(rm, 3), "generated_pct": round(gm, 3),
                        "ratio": round(gm / rm, 2), "recorded_half_spread": round(st.median(x[1] for x in rec[k]), 3),
                        "generated_half_spread": round(st.median(x[1] for x in gen[k]), 3),
                        "recorded_days": len(rec[k]), "seeds": len(gen[k])})
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Generate a simulated session by seed, or compare pricing.")
    ap.add_argument("--cache", default=str(A.DEFAULT_CACHE))
    ap.add_argument("--seed", type=int, help="generate this seed and print its identity")
    ap.add_argument("--version", default=GENERATOR_VERSION, choices=list(SUPPORTED_VERSIONS))
    ap.add_argument("--compare-recorded", action="store_true",
                    help="ATM straddle level/spread: 20 generated seeds vs cached recorded Replay days")
    args = ap.parse_args()
    sim = Simulator(args.cache)
    if args.seed is not None:
        pk = sim.generate(args.seed, args.version)
        print(json.dumps({k: pk["sim"].get(k) for k in ("seed", "generator_version", "inputs_digest", "pack_sha256",
                                                        "pricing_params_sha256", "sigma0", "donor_day", "calibration",
                                                        "parity_residual_usd")}, indent=1))
    if args.compare_recorded:
        for row in compare_with_recorded(sim, Path(args.cache), version=args.version):
            print(row)
