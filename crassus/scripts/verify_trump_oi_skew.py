"""Trump Whisperer x OI skew paper bots (IBIT, SLV, DRAM): entry only on
agreement, the exit rule, retention on missing observations, root isolation
and the runner's snapshot routing. No network: the Trump reading is a fixed
TrumpSentimentSnapshot and the board is a fixture.

    python scripts/verify_trump_oi_skew.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify_oi_skew import FLAT_ROWS, SKEWED_BEARISH_ROWS, SKEWED_BULLISH_ROWS, THIN_ROWS, fresh_quote, make_ctx  # noqa: E402

from crassus import runner  # noqa: E402
from crassus.replay import reset_strategy_state  # noqa: E402
from crassus.strategies import trump_oi_skew as t  # noqa: E402
from crassus.strategy import REGISTRY  # noqa: E402
from crassus.trump_sentiment import TrumpSentimentSnapshot  # noqa: E402

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [OK] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name} -- {detail}")


def mood(mean: float | None, n: int = 3) -> TrumpSentimentSnapshot:
    return TrumpSentimentSnapshot(
        fetched_at="2024-01-01T15:00:00+00:00", sample_size=n, mean_compound=mean,
        bullish_count=n if (mean or 0) > 0 else 0, bearish_count=n if (mean or 0) < 0 else 0,
        neutral_count=0, latest_post_at="2024-01-01T14:55:00+00:00",
    )


def board(root: str, rows: list[dict]) -> list[dict]:
    return [dict(r, OptionSymbol=root + r["OptionSymbol"][3:]) for r in rows]


def ctx(root: str, rows: list[dict], ts: str, held: str | None = None, phase: str = "open"):
    rows = board(root, rows)
    quotes = {r["OptionSymbol"]: fresh_quote(r["OptionSymbol"]) for r in rows}
    trades = [{"sym": held, "side": "buy", "qty": 1, "price": 1.0}] if held else None
    return make_ctx(rows=rows, timestamp=ts, quote_map=quotes, trades=trades, session_phase=phase)


def stamp(minute: int) -> str:
    return f"2024-01-01T{14 + minute // 60:02d}:{minute % 60:02d}:00+00:00"


def main() -> int:
    print("\n-- registration, catalog and runner routing")
    catalog = json.loads((Path(__file__).resolve().parents[1] / "accounts.example.json").read_text(encoding="utf-8"))
    entries = {a["strategy_id"]: a for a in catalog["accounts"]}
    for sid, root in t.ROOTS.items():
        check(f"{sid} is registered", sid in REGISTRY)
        entry = entries.get(sid)
        check(f"{sid} has an inactive paper catalog entry",
              bool(entry) and entry.get("active") is False and entry.get("broker", "worker") == "worker", str(entry))
        check(f"{sid} username fits the 20-character limit", bool(entry) and len(entry["username"]) <= 20)
        check(f"runner routes {sid} to the {root} snapshot", runner.REFERENCE_ROOTS.get(sid) == root)
    check("exactly IBIT, SLV and DRAM", sorted(t.ROOTS.values()) == ["DRAM", "IBIT", "SLV"])

    for sid, root in t.ROOTS.items():
        call = f"{root}240101C00400000"
        put = f"{root}240101P00400000"
        print(f"\n-- {root}: agreement entries")
        reset_strategy_state(sid)
        anchor = t.decide_core(ctx(root, FLAT_ROWS, stamp(0), phase="pre_open"), sid, None, None)
        check("pre-open reading sets the anchor, no trade", anchor.action == "no_trade"
              and anchor.metadata["skew"]["session_start_imbalance"] == 0)
        d = t.decide_core(ctx(root, SKEWED_BULLISH_ROWS, stamp(60)), sid, mood(0.5), None)
        check("both bullish -> buy one ATM call", d.action == "buy" and d.symbol == call and d.quantity == 1, d.reason)
        check("decision is labelled with its own strategy id and root",
              d.strategy_id == sid and d.metadata["root"] == root and d.metadata["agreement"] == "both")

        reset_strategy_state(sid)
        t.decide_core(ctx(root, FLAT_ROWS, stamp(0), phase="pre_open"), sid, None, None)
        d = t.decide_core(ctx(root, SKEWED_BEARISH_ROWS, stamp(60)), sid, mood(-0.5), None)
        check("both bearish -> buy one ATM put", d.action == "buy" and d.symbol == put, d.reason)

        for label, rows, m, expect in (
            ("skew bullish, Trump bearish -> no trade (disagree)", SKEWED_BULLISH_ROWS, mood(-0.5), "disagree"),
            ("skew bullish, Trump neutral -> no trade (skew_only)", SKEWED_BULLISH_ROWS, mood(0.05), "skew_only"),
            ("skew bullish, too few posts -> no trade (skew_only)", SKEWED_BULLISH_ROWS, mood(0.5, n=1), "skew_only"),
            ("skew flat, Trump bullish -> no trade (trump_only)", FLAT_ROWS, mood(0.5), "trump_only"),
            ("thin band, Trump bullish -> no trade (trump_only)", THIN_ROWS, mood(0.5), "trump_only"),
        ):
            reset_strategy_state(sid)
            t.decide_core(ctx(root, FLAT_ROWS, stamp(0), phase="pre_open"), sid, None, None)
            d = t.decide_core(ctx(root, rows, stamp(60)), sid, m, None)
            check(label, d.action == "no_trade" and d.metadata["agreement"] == expect,
                  f"{d.action} {d.metadata.get('agreement')} {d.reason}")
        reset_strategy_state(sid)
        t.decide_core(ctx(root, FLAT_ROWS, stamp(0), phase="pre_open"), sid, None, None)
        d = t.decide_core(ctx(root, SKEWED_BULLISH_ROWS, stamp(60)), sid, None, "feed down")
        check("Trump feed down while flat -> no trade", d.action == "no_trade" and d.metadata["trump"]["lean"] == "unavailable")

        print(f"\n-- {root}: holding and exits")

        def held_case(rows, m, err=None, minute=60, repeat=False):
            reset_strategy_state(sid)
            t.decide_core(ctx(root, FLAT_ROWS, stamp(0), phase="pre_open"), sid, None, None)
            if repeat:
                t.decide_core(ctx(root, rows, stamp(minute), held=call), sid, m, err)
            return t.decide_core(ctx(root, rows, stamp(minute), held=call), sid, m, err)

        d = held_case(SKEWED_BULLISH_ROWS, mood(0.5))
        check("held call, both still bullish -> hold", d.action == "no_trade" and "still supports" in d.reason, d.reason)
        d = held_case(SKEWED_BULLISH_ROWS, mood(0.0, n=0))
        check("held call, Trump goes quiet -> hold (silence never closes)", d.action == "no_trade", d.reason)
        d = held_case(SKEWED_BULLISH_ROWS, mood(-0.5))
        check("held call, Trump flips bearish -> sell", d.action == "sell" and d.symbol == call, d.reason)
        d = held_case(FLAT_ROWS, mood(0.5))
        check("held call, skew goes neutral -> sell", d.action == "sell" and d.symbol == call, d.reason)
        d = held_case(SKEWED_BEARISH_ROWS, mood(0.5))
        check("held call, skew flips bearish -> sell", d.action == "sell", d.reason)
        d = held_case(FLAT_ROWS, None, err="feed down")
        check("held call, Trump feed down -> retain even on a neutral skew", d.action == "no_trade" and "retaining" in d.reason, d.reason)
        d = held_case(FLAT_ROWS, mood(0.5), repeat=True)
        check("held call, duplicate snapshot -> retain (no new observation)", d.action == "no_trade" and "retaining" in d.reason, d.reason)

        print(f"\n-- {root}: isolation")
        reset_strategy_state(sid)
        d = t.decide_core(ctx("QQQ", SKEWED_BULLISH_ROWS, stamp(60)), sid, mood(0.5), None)
        check("a QQQ board is rejected", d.action == "no_trade" and d.metadata["skew"]["lean"] == "unavailable")
        other = next(r for r in t.ROOTS.values() if r != root)
        d = t.decide_core(ctx(root, SKEWED_BULLISH_ROWS, stamp(60), held=f"{other}240101C00400000"), sid, mood(0.5), None)
        check(f"a held {other} contract is not this bot's", d.action == "no_trade" and d.metadata["skew"]["lean"] == "unavailable")
        d = t.decide_core(ctx(root, SKEWED_BULLISH_ROWS, stamp(61), phase="closed"), sid, mood(0.5), None)
        check("closed market -> no trade", d.action == "no_trade")

    print("\n-- the anchor is not shared with the root's reference skew account")
    reset_strategy_state("trump_whisperer_oi_skew_ibit")
    reset_strategy_state("oi_skew_ibit_adaptive")
    REGISTRY["oi_skew_ibit_adaptive"](ctx("IBIT", SKEWED_BULLISH_ROWS, stamp(0), phase="pre_open"))
    d = t.decide_core(ctx("IBIT", FLAT_ROWS, stamp(0), phase="pre_open"), "trump_whisperer_oi_skew_ibit", None, None)
    check("Trump IBIT anchor is its own first reading, not the reference account's",
          d.metadata["skew"]["session_start_imbalance"] == 0)

    print("\n" + "=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
