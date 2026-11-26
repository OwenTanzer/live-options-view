"""Paired counterfactual manager/dealer ledgers for the same exogenous observations.

Run from repo root: python -m research.options_replay.participant_models [scenario.json]
See participant_models.md for the observation contract. Uses only the standard library.
"""
import argparse
import itertools
import json
import math
from pathlib import Path

MANAGERS = ("scheduled", "flow_adjusted")
DEALERS = ("delta_target", "delta_band")


def number(value, label):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return value


def contracts(value, label):
    if type(value) is not int:
        raise ValueError(f"{label} must be an integer")
    return value


def simulate(scenario, manager, dealer):
    """Return one path; every state update uses only the current and earlier rows."""
    if manager not in MANAGERS or dealer not in DEALERS:
        raise ValueError("unknown manager or dealer")
    band = number(scenario["band_shares"], "band_shares")
    multiplier = contracts(scenario["multiplier"], "multiplier")
    if band < 0 or multiplier <= 0:
        raise ValueError("band must be nonnegative and multiplier positive")
    ratios = scenario["legs"]
    if not ratios or any(not contracts(q, "leg ratio") for q in ratios.values()):
        raise ValueError("supply at least one leg with nonzero integer ratio")
    if not scenario["steps"]:
        raise ValueError("supply at least one observation")
    units = 0
    hedge = 0.0
    rows = []
    previous = None
    for step in scenario["steps"]:
        tick = number(step["tick"], "tick")
        if previous is not None and tick <= previous:
            raise ValueError("ticks must be strictly increasing")
        reset = step.get("reset_units")
        flow = contracts(step.get("cash_flow_units", 0), "cash_flow_units")
        if previous is None and reset is None:
            raise ValueError("first observation must initialize a new series")
        if reset is not None:
            units = 0  # Old options cash-settle. No closing print is generated.
            base = contracts(reset, "reset_units")
            if base < 0:
                raise ValueError("reset_units must be nonnegative")
        else:
            base = units
        target = base + (flow if manager == "flow_adjusted" else 0)
        if target < 0:
            raise ValueError("redemptions exceed outstanding package units")
        if set(step["deltas"]) != set(ratios):
            raise ValueError("each step must supply a delta for every leg")
        deltas = {leg: number(d, "delta") for leg, d in step["deltas"].items()}
        if any(abs(d) > 1 for d in deltas.values()):
            raise ValueError("vanilla option delta outside [-1, 1]")
        fills = []
        for leg, ratio in ratios.items():
            quantity = (target - units) * ratio
            if quantity:
                fills.append({"leg": leg, "manager": quantity, "dealer": -quantity,
                              "option_volume": abs(quantity)})
        # Dealer takes the opposite option legs. Its neutralizing stock holding
        # equals the manager's signed option delta, converted into shares.
        wanted = target * multiplier * sum(ratios[leg] * deltas[leg] for leg in ratios)
        gap = wanted - hedge
        width = band if dealer == "delta_band" else 0
        order = math.copysign(max(0.0, abs(gap) - width), gap)
        hedge += order
        rows.append({"tick": tick, "new_series": reset is not None, "units": target,
                     "matched_legs": fills, "dealer_stock_order": order,
                     "dealer_stock_position": hedge, "residual_delta_shares": hedge - wanted})
        units = target
        previous = tick
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", nargs="?", type=Path,
                        default=Path(__file__).with_name("participant_example.json"))
    args = parser.parse_args()
    scenario = json.loads(args.scenario.read_text())
    for manager, dealer in itertools.product(MANAGERS, DEALERS):
        print(json.dumps({"manager": manager, "dealer": dealer,
                          "rows": simulate(scenario, manager, dealer)}, allow_nan=False))


if __name__ == "__main__":
    main()
