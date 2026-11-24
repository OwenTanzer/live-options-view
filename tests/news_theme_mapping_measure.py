"""Reproducible local pilot workload estimate; no network or archive writes."""
import gzip
import json
import statistics
import time

from test_news_theme_mapping import CASES, WHEN
from market_data.news.classify import classify_rules
from market_data.news.normalize import canonical_json


def main():
    observations = [row["observation"] for row in CASES]
    # Repeated representative mix, explicitly not an observed traffic distribution.
    samples = {False: [], True: []}
    for _ in range(5):
        for enabled in (False, True):
            start = time.perf_counter()
            for _ in range(100):
                for obs in observations:
                    classify_rules(obs, "fixture-replay", WHEN, fed_treasury_pilot=enabled)
            samples[enabled].append((time.perf_counter() - start) / (100 * len(observations)))
    payloads = {}
    for enabled in (False, True):
        payloads[enabled] = ("\n".join(canonical_json(classify_rules(
            obs, "fixture-replay", WHEN, fed_treasury_pilot=enabled))
            for obs in observations) + "\n").encode()
    extra_bytes = len(payloads[True]) - len(payloads[False])
    us = (statistics.median(samples[True]) - statistics.median(samples[False])) * 1e6
    # Conservative planning assumption: 2 KB extra per revision (above this fixture
    # mean), every revision retained for a full month, no free-tier deduction.
    monthly_revisions = 35 * 30
    print(json.dumps({
        "fixture_count": len(observations), "trials": 5, "classifications_per_arm_per_trial": 1600,
        "median_us_off": statistics.median(samples[False]) * 1e6,
        "median_us_on": statistics.median(samples[True]) * 1e6,
        "incremental_us_per_revision": us,
        "incremental_raw_bytes_batch": extra_bytes,
        "incremental_raw_bytes_per_revision": extra_bytes / len(observations),
        "incremental_gzip_bytes_batch": len(gzip.compress(payloads[True], mtime=0)) - len(gzip.compress(payloads[False], mtime=0)),
        "assumptions": {"revisions_per_day": 35, "days": 30, "extra_bytes_per_revision": 2000,
                        "r2_usd_per_gb_month": 0.015, "cycles_per_month": 2880},
        "projected_incremental_cpu_seconds_month": us * monthly_revisions / 1e6,
        "projected_extra_raw_bytes_month": monthly_revisions * 2000,
        "projected_storage_usd_full_month_unrounded": monthly_revisions * 2000 / 1e9 * 0.015,
        "current_pilot_remote_requests": 0, "current_pilot_incremental_hosted_cost_usd": 0,
    }, indent=2))


if __name__ == "__main__":
    main()
