"""Emit fixed-fixture strategy/execution outputs for cross-revision comparison.

Usage: python scripts/fixtures/snapshot_lineage_behavior.py /path/to/crassus
Run against base and candidate checkouts, then compare stdout byte-for-byte.
Uses only fake sessions; never contacts a service. Intentionally compatible
with the pre-lineage base so the reference is independently executable.
"""
import json
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

root = Path(sys.argv[1]).resolve()
sys.path[:0] = [str(root), str(root / "scripts")]
from crassus.client import AccountState, ExecutionClient
from crassus.market import MarketSnapshot
from verify_runner_flatten_attribution import FakeAccount, make_runner

account = FakeAccount("fixture", "fixture", "smoke_atm_roundtrip")
now = datetime(2026, 10, 2, 12, tzinfo=ZoneInfo("America/New_York"))
symbol = "QQQ261002C00400000"
row = {"OptionSymbol": symbol, "Strike": 400.0, "Type": "call",
       "Bid": 1.0, "Ask": 1.1, "IV": 0.2}
trade = {"sym": symbol, "side": "buy", "qty": 1, "price": 1.0,
         "strike": 400.0, "type": "call", "exp": "2026-10-02",
         "instrument_type": "option", "multiplier": 100,
         "ts": "2026-10-02T15:00:00Z"}
outputs = []
for name, phase, rows, trades, dry, hour in (
    ("buy", "open", [row], [], False, 12),
    ("sell", "open", [row], [trade], False, 12),
    ("no_trade", "open", [], [], False, 12),
    ("closed", "closed", [row], [], False, 12),
    ("dry_run", "open", [row], [], True, 12),
    ("flatten", "open", [row], [trade], False, 15),
):
    for key_fields in ({}, {"snapshot_key": "intraday/20261002/snapshot_120000123456.csv"},
                       {"snapshot_key": {"invalid": "metadata"}}):
        payload = {"timestamp": "2026-10-02T16:00:00.123499+00:00",
                   "expiration": "2026-10-02", "underlying_price": 400.0,
                   "rows": rows, **key_fields}
        snapshot = MarketSnapshot.from_payload("test://latest.json", payload, json.dumps(payload).encode())
        state = AccountState("fixture", 10000.0, trades, fetched_at="2026-10-02T16:00:00+00:00")
        with tempfile.TemporaryDirectory() as tmp:
            runner, executor = make_runner(account, state=state, ledger_dir=Path(tmp))
            runner.dry_run = dry
            with patch("crassus.clock.now_et", return_value=now.replace(hour=hour, minute=50 if hour == 15 else 0)):
                runner._run_account(account, snapshot, phase)
            rec = json.loads(runner.ledger.paths.ledger.read_text().splitlines()[-1])
            projected = {k: rec.get(k) for k in ("decision", "reason", "outcome_class",
                         "strategy_id", "strategy_version", "account_strategy_id",
                         "account_state_before", "account_state_after", "http_status")}
            bodies = []
            for submitted in executor.submit_calls:
                session = SimpleNamespace(account=account, _mutation_lock=threading.Lock(),
                    me=Mock(return_value=state), _request=Mock(return_value=Mock(status_code=200, json=lambda: {})))
                client = ExecutionClient(session, Path(tmp) / "state")
                client.submit(**submitted, execution_request_id="fixture-fixed-request")
                bodies.append(session._request.call_args.kwargs["json"])
            outputs.append({"scenario": name, "payload": payload, "rows": snapshot.rows,
                            "underlying_price": snapshot.underlying_price,
                            "record": projected, "http_bodies": bodies})
print(json.dumps(outputs, sort_keys=True, indent=2))
