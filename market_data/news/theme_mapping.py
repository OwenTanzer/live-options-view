"""Local, opt-in headline relevance pilot. No fetching, prices or direction claims."""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path

from .normalize import canonical_json, sha256_hex

VERSION = "fed-treasury-v1"
# Frozen policy: later source enablement must not silently expand this pilot.
SOURCE_IDS = ("ars_technica", "eia_today_in_energy", "oilprice_com", "techcrunch")
# Explicit US institutions only. Bare 'fed', 'Treasury', Powell, rates and debt
# are ambiguous. Patterns describe topics, including negation/speculation.
RULES = (
    ("fed_policy", r"\b(?:Federal Reserve|FOMC|Federal Open Market Committee)\b",
     ("QQQ", "SPY", "TLT")),
    ("treasury_financing", r"\b(?:U\.?S\.?|United States) Treasury\b.{0,100}\b(?:auctions?|debt|bonds?|notes?|bills?|refunding|borrowing|general account|TGA|deficits?)\b",
     ("TLT",)),
    ("treasury_financing", r"\b(?:auctions?|debt|bonds?|notes?|bills?|refunding|borrowing|general account|TGA|deficits?)\b.{0,100}\b(?:U\.?S\.?|United States) Treasury\b",
     ("TLT",)),
)
DIGEST = sha256_hex(canonical_json({
    "version": VERSION, "source_ids": SOURCE_IDS, "field": "title",
    "flags": "IGNORECASE", "rules": RULES,
    "meaning": "topic_relevance_only",
}))
_COMPILED = tuple((theme, re.compile(pattern, re.IGNORECASE), tickers)
                  for theme, pattern, tickers in RULES)
ATTRIBUTION_FIELDS = (
    "source_id", "source_name", "original_url", "canonical_url", "source_guid",
    "published_raw", "published_at", "updated_raw", "updated_at", "time_quality",
    "observed_at", "first_seen_at", "archived_at",
)


def apply_mapping(classification: dict, observation: dict) -> dict:
    """Create a separately identified enrichment; leave the observation untouched."""
    result = dict(classification)
    version = f"{classification['version']}+{VERSION}"
    digest = sha256_hex(canonical_json({"base_rules": classification["rules_digest"],
                                        "mapping": DIGEST}))
    matches, tickers = [], set()
    eligible = observation.get("source_id") in SOURCE_IDS
    if eligible:
        for theme, pattern, instruments in _COMPILED:
            match = pattern.search(observation.get("title", ""))
            if match:
                matches.append({"theme": theme, "field": "title", "start": match.start(),
                                "end": match.end(), "text": match.group()})
                tickers.update(instruments)
    result.update({
        "version": version, "rules_digest": digest,
        "classification_id": sha256_hex(
            f"{observation['revision_id']}\x00rules\x00{version}\x00{digest}"),
        "instruments": sorted(tickers),
        "instrument_mapping": "theme_relevance" if matches else "unknown",
        "theme_mapping": {
            "version": VERSION, "digest": DIGEST, "meaning": "topic_relevance_only",
            "basis": "headline_rule" if matches else ("no_rule" if eligible else "source_out_of_scope"),
            "themes": sorted({m["theme"] for m in matches}), "matches": matches,
            "attribution": {key: observation[key] for key in ATTRIBUTION_FIELDS if key in observation},
        },
    })
    return result


def main():
    """Replay local normalized JSONL to stdout. No archive/store dependency."""
    from .classify import classify_rules
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observations", type=Path)
    parser.add_argument("--classified-at", required=True, help="Actual replay time with timezone")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--enable-fed-treasury", action="store_true", default=False)
    args = parser.parse_args()
    when = datetime.fromisoformat(args.classified_at.replace("Z", "+00:00"))
    if when.tzinfo is None:
        parser.error("--classified-at requires a timezone")
    with args.observations.open(encoding="utf-8") as source:
        for line in source:
            if line.strip():
                print(canonical_json(classify_rules(json.loads(line), args.run_id, when,
                      fed_treasury_pilot=args.enable_fed_treasury)))


if __name__ == "__main__":
    main()
