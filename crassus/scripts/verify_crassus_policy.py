#!/usr/bin/env python3
"""Prove the Crassus AI policy layer's fail-closed guarantees.

Pure-Python checks against `policy.OverridePolicy.evaluate()` directly --
no server, no network. Each scenario is a claim collaborators required
before any Crassus AI proposal is allowed to reach a strategy's params;
the point is demonstrating each one holds, not coverage for its own sake.

    python scripts/verify_crassus_policy.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.config import Account  # noqa: E402
from crassus.policy import OverridePolicy  # noqa: E402

passed, failed = 0, 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [✓] {name}" + (f" -- {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  [✗] {name}" + (f" -- {detail}" if detail else ""))


ACCOUNT = Account(
    alias="momentum_bot",
    username="momentum_bot",
    password="unused",
    strategy_id="momentum_qqq",
    params={"bullish_threshold": 0.003, "bearish_threshold": -0.003},
)

SIBLING = Account(
    alias="other_bot",
    username="other_bot",
    password="unused",
    strategy_id="momentum_qqq",
    params={"bullish_threshold": 0.003, "bearish_threshold": -0.003},
)

BASELINE = dict(ACCOUNT.params)


def future(minutes: float = 60.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def past(minutes: float = 60.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def envelope(**overrides: Any) -> dict[str, Any]:
    base = dict(
        id="ov-1",
        account_alias=ACCOUNT.alias,
        status="accepted",
        proposed_params={"bullish_threshold": 0.004},
        expires_utc=future(),
    )
    base.update(overrides)
    return base


def evaluate(env: dict[str, Any] | None, **kw: Any):
    policy = OverridePolicy()
    kw.setdefault("kill_switch", False)
    kw.setdefault("frozen", False)
    kw.setdefault("prior_accepted_params", None)
    prior = kw.pop("prior_accepted_params")
    account = kw.pop("account", ACCOUNT)
    return policy.evaluate(account, BASELINE, env, prior, **kw)


def main() -> int:
    print("Crassus AI policy layer -- fail-closed guarantees\n")

    # 1. Unknown key anywhere in proposed_params -> whole envelope rejected.
    r = evaluate(envelope(proposed_params={"bullish_threshold": 0.004, "symbol": "QQQ260101C00500000"}))
    check(
        "1. Unknown/forbidden key rejects the whole envelope",
        not r.applied and r.effective_params == BASELINE and "symbol" not in r.effective_params,
        f"applied={r.applied} rejections={r.rejections}",
    )

    # 2. A battery of forbidden field names smuggled into proposed_params,
    #    none of which exist in any strategy's ParamSpec allowlist.
    forbidden = {
        "symbol": "QQQ260101C00500000", "quantity": 5, "action": "buy",
        "strategy_id": "momentum_qqq", "account_alias": "someone_else",
        "execution_request_id": "abc", "password": "x", "username": "x",
        "api_endpoint": "https://evil.example/", "webhook_url": "https://evil.example/",
    }
    all_rejected = True
    for key, value in forbidden.items():
        r = evaluate(envelope(proposed_params={key: value}))
        if r.applied or key in r.effective_params:
            all_rejected = False
    check("2. Forbidden fields cannot cross into effective_params", all_rejected)

    # 3. Out-of-bounds numeric value.
    r = evaluate(envelope(proposed_params={"bullish_threshold": 5.0}))  # way above max=0.02
    check("3. Out-of-bounds value rejects the envelope", not r.applied, f"rejections={r.rejections}")

    # 4. Rate-of-change cap vs. prior accepted value.
    r = evaluate(
        envelope(proposed_params={"bullish_threshold": 0.019}),  # in-bounds but >50% jump from 0.003
        prior_accepted_params={"bullish_threshold": 0.003},
    )
    check("4. Excessive rate-of-change rejects the envelope", not r.applied, f"rejections={r.rejections}")

    # 5. Expired envelope.
    r = evaluate(envelope(expires_utc=past()))
    check("5. Expired envelope rejects", not r.applied)

    # 6. Malformed inputs never raise and always fail closed.
    exceptions = []
    results = []
    for bad in (None, "not a dict", 42, {"status": "accepted"}, {"proposed_params": "nope", "status": "accepted", "expires_utc": future(), "account_alias": ACCOUNT.alias}):
        try:
            results.append(evaluate(bad))
        except Exception as exc:  # the whole point of this case
            exceptions.append(exc)
    check(
        "6. Malformed/None/wrong-type envelopes never raise, always baseline",
        not exceptions and all(r.effective_params == BASELINE and not r.applied for r in results),
        f"exceptions={exceptions}",
    )

    # 7. status != accepted.
    all_rejected = True
    for status in ("proposed", "rejected", "expired", "superseded", "bogus"):
        r = evaluate(envelope(status=status))
        if r.applied:
            all_rejected = False
    check("7. Non-accepted status always rejects", all_rejected)

    # 8. Kill switch wins over an otherwise-valid envelope.
    r = evaluate(envelope(), kill_switch=True)
    check("8. Kill switch forces baseline regardless of a valid envelope", not r.applied and r.effective_params == BASELINE)
    r_ambiguous = evaluate(envelope(), kill_switch=None)
    check("8b. Unreachable kill-switch state (None) is treated as engaged", not r_ambiguous.applied)

    # 9. Per-bot freeze only affects that account.
    r_frozen = evaluate(envelope(), frozen=True)
    r_sibling = evaluate(envelope(account_alias=SIBLING.alias), account=SIBLING, frozen=False)
    check(
        "9. Freeze affects only the frozen account, not a sibling",
        not r_frozen.applied and r_sibling.applied,
        f"frozen.applied={r_frozen.applied} sibling.applied={r_sibling.applied}",
    )

    # 10. A fully valid envelope merges cleanly and touches only allowlisted keys.
    r = evaluate(envelope(proposed_params={"bullish_threshold": 0.0035}))
    check(
        "10. Valid in-bounds accepted envelope merges cleanly",
        r.applied
        and r.effective_params["bullish_threshold"] == 0.0035
        and r.effective_params["bearish_threshold"] == BASELINE["bearish_threshold"],
        f"effective_params={r.effective_params}",
    )

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
