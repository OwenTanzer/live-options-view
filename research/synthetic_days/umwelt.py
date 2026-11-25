"""Toy QQQ event study for OA-145. All coefficients and events are synthetic.

Run: python research/synthetic_days/umwelt.py --seed 247 --sessions 100
One tick represents one minute. Output returns are signed additive basis points.
"""
import argparse
import csv
import random
import sys
from collections import defaultdict
from statistics import fmean


def simulate(sessions, seed, shared, impact_bp, bogdanoff):
    """Run paired worlds using identical exogenous shocks and decision times."""
    rng = random.Random(seed)
    days = []
    for _ in range(sessions):
        returns = []
        events = []
        for minute in range(390):
            common_shock = rng.gauss(0, 3)
            independent_signal = rng.gauss(0, 1)
            peer_noise = rng.gauss(0, 0.6)
            receives_opportunity = rng.random() < 0.025
            cue = sum(returns[-3:])
            peer_flow = (1 if cue > 0 else -1 if cue < 0 else 0) + peer_noise
            perceived = cue / 5.2 if shared else independent_signal
            # A common threshold links timing as well as side to the visible cue.
            active_signal = abs(perceived) >= 0.6
            own = 0
            if receives_opportunity and 5 <= minute <= 385 and active_signal:
                own = 1 if perceived > 0 else -1
                events.append((minute, own))
            # Aggregate liquidity and positioning response. At -1 it absorbs
            # peer pressure completely. At +1 it doubles transmission.
            pressure_bp = 0.8 * (1 + bogdanoff) * peer_flow
            returns.append(common_shock + pressure_bp + impact_bp * own)
        days.append((returns, events))
    return days


def summarize(days):
    # The same clock minute on a different day is the placebo, with the
    # original side retained. Exclude controls with a simulated action nearby.
    # This first demonstration matches clock time only.
    total = defaultdict(list)
    for day, (returns, events) in enumerate(days):
        control_returns, control_events = days[(day + 1) % len(days)]
        occupied = {minute for minute, _ in control_events}
        for minute, side in events:
            if any(t in occupied for t in range(minute - 5, minute + 5)):
                continue
            for name, start, end in [('prior_5m', -5, 0), ('execution_minute', 0, 1),
                                     ('next_4m', 1, 5)]:
                diff = sum(returns[minute + start:minute + end])
                diff -= sum(control_returns[minute + start:minute + end])
                total[name].append(side * diff)
    return total


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--seed', type=int, default=247)
    p.add_argument('--sessions', type=int, default=100)
    p.add_argument('--bogdanoff', type=float, default=-0.5,
                   help='residual liquidity response to peer pressure (-1 absorbs, +1 amplifies)')
    args = p.parse_args()
    if args.sessions < 2 or not -1 <= args.bogdanoff <= 1:
        p.error('use at least two sessions and a Bogdanoff coefficient between -1 and 1')
    writer = csv.writer(sys.stdout, lineterminator='\n')
    writer.writerow(['model', 'seed', 'sessions', 'bogdanoff', 'own_impact_bp',
                     'window', 'eligible_events', 'signed_excess_bp'])
    for name, shared, impact in [('independent', False, 0.0), ('shared_cue', True, 0.0),
                                 ('own_impact', False, 0.25), ('both', True, 0.25)]:
        totals = summarize(simulate(args.sessions, args.seed, shared, impact, args.bogdanoff))
        for window in ('prior_5m', 'execution_minute', 'next_4m'):
            values = totals[window]
            writer.writerow([name, args.seed, args.sessions, args.bogdanoff, impact,
                             window, len(values), round(fmean(values), 4) if values else ''])


if __name__ == '__main__':
    main()
