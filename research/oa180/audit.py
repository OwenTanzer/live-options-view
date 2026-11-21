"""Replay verified production segments, in receipt order, using only prior observations."""
from collections import Counter, defaultdict
from datetime import datetime
import gzip
import hashlib
import json
from pathlib import Path
import sys

from fetch import RUNS


MANIFEST_HASHES = {
    "2026-09-25": "727933ec55d1710f977175887d0090fa70086abaa360e2ff8c5acc3097e4349f",
    "2026-09-29": "e40c6a01de3eccf5ecf6437152f885b7760aa5a2dd5033bd40fbf5f744a0583d",
    "2026-10-01": "19e8f99d30b80b331e04c1e2526f4eabb996a0953a645ba6ce004b0cec2f977a",
}


def audit(root, day, owner):
    manifest = root / f"moo144/tradier/{day}/manifest-{owner}.json"
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == MANIFEST_HASHES[day]
    artifacts = json.loads(manifest.read_bytes())["artifacts"]
    summary = json.loads((manifest.parent / f"summary-{owner}.json").read_bytes())
    universe = summary["universe"]["option_metadata"]
    assert len(universe) == 16 and summary["status"] == "complete"
    assert not any(summary[k] for k in ("partial_reasons", "gap_seconds", "effective_late_start_seconds", "needs_review", "upload_failures"))
    summary_counts = Counter()
    symbols = Counter()
    tables = defaultdict(Counter)
    examples = {}
    prior = {}
    quotes = {}
    fields = Counter()
    exclusions = Counter()
    seen = set()
    volume = 0
    first = last = None
    market_open, market_close = [datetime.fromisoformat(summary[k]).timestamp()*1000 for k in ("session_open","session_close")]
    for artifact in sorted(artifacts, key=lambda item: item["key"]):
        data = (root / artifact["key"]).read_bytes()
        assert len(data) == artifact["bytes"] and hashlib.sha256(data).hexdigest() == artifact["sha256"]
        if "records" not in artifact:
            continue
        lines = gzip.decompress(data).splitlines()
        assert len(lines) == artifact["records"]
        for line_number, line in enumerate(lines, 1):
            e = json.loads(line)
            kind = e["type"]
            summary_counts[kind] += 1
            if kind == "quote":
                quotes[e["symbol"]] = e
            if kind != "timesale":
                continue
            symbol = e["symbol"]
            symbols[symbol] += 1
            if symbol == "QQQ":
                continue
            assert symbol in universe
            fields.update(k for k,v in e.items() if v is not None)
            ts = int(e["date"])
            receipt = e["collector_receipt_timestamp"]
            price = round(float(e["last"])*10000)
            size = int(e["size"])
            identity = (symbol,e.get("seq"))
            reason = next((k for k in ("cancel","correction","duplicate_in_run") if e.get(k)), None)
            if reason or identity[1] is None or identity in seen or not market_open <= ts <= market_close or size <= 0:
                exclusions[reason or "ambiguous_sequence_or_outside_session"] += 1
                continue
            seen.add(identity)
            first = min(first or ts, ts)
            last = max(last or ts, ts)
            volume += size
            tick = "unknown"
            previous = prior.get(symbol)
            if previous and ts > previous[0]:
                tick = "buy" if price > previous[1] else "sell" if price < previous[1] else previous[2]
            if not previous or ts >= previous[0]:
                prior[symbol] = (ts, price, tick)
            quote = quotes.get(symbol) or e.get("preceding_quote_context") or {}
            age = spread = midpoint_delta = None
            if all(quote.get(k) is not None for k in ("bid","ask","biddate","askdate","collector_receipt_timestamp")):
                bid, ask = [round(float(quote[k])*10000) for k in ("bid","ask")]
                side_dates = [int(quote[k]) for k in ("biddate","askdate")]
                if (quote["symbol"] == symbol and quote["collector_receipt_timestamp"] < receipt
                    and datetime.fromisoformat(quote["collector_receipt_timestamp"]).timestamp()*1000 < ts
                    and max(side_dates) < ts and 0 < bid < ask):
                    age = ts - min(side_dates)
                    spread = ask-bid
                    midpoint_delta = 2*price-bid-ask
            def quote_sign(max_age, max_spread=None):
                if age is None or age > max_age or (max_spread is not None and spread > max_spread):
                    return "unknown"
                return "buy" if midpoint_delta > 0 else "sell" if midpoint_delta < 0 else "unknown"
            q = quote_sign(1000)
            combined = q if q != "unknown" else tick
            for method, sign in (("quote_100ms",quote_sign(100)),("quote_1s",q),("quote_5s",quote_sign(5000)),
                                 ("quote_1s_spread_le_10c",quote_sign(1000,1000)),("tick",tick),("combined",combined)):
                tables[method][sign] += size
            pair = "one_or_both_unknown" if "unknown" in (q,tick) else "agree" if q == tick else "disagree"
            tables["comparison"][pair] += size
            if pair not in examples and previous and quote:
                examples[pair] = dict(segment=artifact["key"],line=line_number,symbol=symbol,trade_ms=ts,
                    receipt=receipt,price=price/10000,previous_tick=previous,bid=quote.get("bid"),ask=quote.get("ask"),
                    quote_receipt=quote.get("collector_receipt_timestamp"),age_ms=age,quote_sign=q,tick_sign=tick)
    assert summary_counts == summary["event_counts"] and symbols == summary["timesale_counts_by_symbol"]
    assert set(symbols) == set(universe) | {"QQQ"} and all(sum(row.values()) == volume for row in tables.values())
    return dict(day=day,manifest=str(manifest.relative_to(root)),manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
                universe=sorted(universe),first_last_option_ms=[first,last],event_counts=summary_counts,
                accepted_contracts=volume,excluded_option_prints=exclusions,fields_populated=fields,
                provider_audit={k:summary[k] for k in ("duplicate_count","cancel_count","correction_count","gap_seconds","sequence_out_of_order_by_symbol","sequence_discontinuities_by_symbol")},
                contracts_by_method=tables,examples=examples)

if __name__ == "__main__":
    print(json.dumps([audit(Path(sys.argv[1]), d, o) for d,o in RUNS.items()],indent=2,sort_keys=True))
