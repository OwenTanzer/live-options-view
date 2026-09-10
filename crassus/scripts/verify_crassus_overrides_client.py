#!/usr/bin/env python3
"""Prove overrides_client.OverridesClient bounds a whole run's worth of
network work, not just one request -- the gap flagged in review: an
unbounded Worker outage previously cost every account its own full timeout
on every one of 3 GETs + 1 POST per cycle (22 accounts x 4 calls could mean
88 real timeout attempts, comfortably past the runner's cadence and
watchdog).

Hermetic: no real network. `requests.Session` is swapped for a fake whose
`.get`/`.post` either raise (simulating an unreachable Worker) or record
calls, and `monotonic` is injected so the circuit's cooldown never needs a
real sleep.

    python scripts/verify_crassus_overrides_client.py
"""

from __future__ import annotations

import queue
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crassus.overrides_client import OverridesClient  # noqa: E402

passed, failed = 0, 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [OK] {name}" + (f" -- {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class AlwaysFailsSession:
    """Every .get()/.post() raises, as if the Worker were completely
    unreachable -- no attribute on `requests.Session` this client doesn't
    already call is exercised, so nothing else needs faking."""

    def __init__(self):
        self.get_calls = 0
        self.post_calls = 0

    def get(self, *a: Any, **kw: Any):
        self.get_calls += 1
        raise ConnectionError("simulated: Worker unreachable")

    def post(self, *a: Any, **kw: Any):
        self.post_calls += 1
        raise ConnectionError("simulated: Worker unreachable")


class RecordingSession:
    def __init__(self):
        self.get_calls: list[str] = []
        self.post_calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kw: Any):
        self.get_calls.append(url)
        raise ConnectionError("simulated: Worker unreachable")

    def post(self, url: str, json: dict, **kw: Any):
        self.post_calls.append((url, json))

        class _Resp:
            status_code = 200

        return _Resp()


class _Resp:
    def __init__(self, status_code: int, payload: Any = None, json_error: bool = False):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("simulated: not valid JSON")
        return self._payload


class SlowServerErrorSession:
    """Every .get()/.post() completes -- no exception -- but with HTTP 503
    after a simulated delay, as if the Worker itself were up but failing.
    This is the exact shape a bare consecutive-*exception* circuit cannot
    see: nothing here ever raises, so only status-code-aware failure
    classification (not "did the request throw") can detect it."""

    def __init__(self, clock: FakeClock, delay_s: float, status_code: int = 503):
        self.clock = clock
        self.delay_s = delay_s
        self.status_code = status_code
        self.get_calls = 0
        self.post_calls = 0

    def get(self, *a: Any, **kw: Any):
        self.get_calls += 1
        self.clock.advance(self.delay_s)
        return _Resp(self.status_code)

    def post(self, *a: Any, **kw: Any):
        self.post_calls += 1
        self.clock.advance(self.delay_s)
        return _Resp(self.status_code)


class ScriptedSession:
    """Returns one canned response per call from `responses`, in order, and
    advances the fake clock by `delay_s` on every call regardless of what
    it returns -- for scenarios that need a specific success/failure
    sequence rather than a uniform one."""

    def __init__(self, clock: FakeClock, responses: list, delay_s: float = 0.0):
        self.clock = clock
        self._responses = list(responses)
        self.delay_s = delay_s
        self.get_calls = 0

    def get(self, *a: Any, **kw: Any):
        self.get_calls += 1
        self.clock.advance(self.delay_s)
        resp = self._responses[min(self.get_calls - 1, len(self._responses) - 1)]
        if resp is ConnectionError:
            raise ConnectionError("simulated: Worker unreachable")
        return resp


def scenario_circuit_bounds_repeated_failures_across_many_accounts() -> None:
    print("\n1. Circuit breaker: a Worker outage costs a bounded number of real attempts, not one per account")
    clock = FakeClock()
    session = AlwaysFailsSession()
    client = OverridesClient(
        base_url="http://fake", bot_registration_key="k",
        enabled=True, failure_circuit_threshold=3, circuit_cooldown_s=60.0,
        monotonic=clock, start_mirror_thread=False,
    )
    client.http = session

    # Simulate 22 accounts, each making its 3 GETs per cycle (66 total
    # calls) against a Worker that is completely down for the whole run.
    for _ in range(22):
        client.fetch_override("some_account")
        client.fetch_kill_switch()
        client.fetch_freeze("some_account")

    check(
        "the circuit opens after the configured threshold and stops making real requests",
        session.get_calls == 3,
        f"real GET attempts: {session.get_calls} (expected exactly the failure threshold, not 66)",
    )
    check("fetch_override degrades to None (fail-closed) once the circuit is open", client.fetch_override("x") is None)
    check("fetch_kill_switch degrades to None (fail-closed) once the circuit is open", client.fetch_kill_switch() is None)
    check("fetch_freeze degrades to None (fail-closed) once the circuit is open", client.fetch_freeze("x") is None)

    # The circuit reopens for real attempts once its cooldown elapses.
    clock.advance(61.0)
    client.fetch_kill_switch()
    check(
        "the circuit allows a real attempt again once the cooldown elapses",
        session.get_calls == 4,
        session.get_calls,
    )


def scenario_disabled_client_makes_zero_network_calls() -> None:
    print("\n2. The no-traffic disabled path: enabled=False never touches the network at all")
    session = AlwaysFailsSession()
    client = OverridesClient(
        base_url="http://fake", bot_registration_key="k", enabled=False, start_mirror_thread=False,
    )
    client.http = session

    for _ in range(5):
        client.fetch_override("acct")
        client.fetch_kill_switch()
        client.fetch_freeze("acct")
    client.post_ledger_mirror({"decision_id": "d1"})

    check("no GET was ever attempted while disabled", session.get_calls == 0, session.get_calls)
    check("no POST was ever attempted while disabled", session.post_calls == 0, session.post_calls)
    check("post_ledger_mirror is a silent no-op while disabled (nothing queued)", client._mirror_queue.qsize() == 0)


def scenario_ledger_mirror_never_blocks_the_caller() -> None:
    print("\n3. post_ledger_mirror enqueues and returns immediately, even though the actual POST is never attempted here")
    client = OverridesClient(
        base_url="http://fake", bot_registration_key="k", enabled=True, start_mirror_thread=False,
    )
    client.http = AlwaysFailsSession()

    started = time.monotonic()
    for i in range(10):
        client.post_ledger_mirror({"decision_id": f"d{i}"})
    elapsed = time.monotonic() - started

    check("ten enqueues complete near-instantly (no network I/O on the caller's thread)", elapsed < 0.5, f"{elapsed:.3f}s")
    check("all ten records are queued for the (unstarted) background worker", client._mirror_queue.qsize() == 10)


def scenario_full_mirror_queue_drops_oldest_rather_than_blocking() -> None:
    print("\n4. A full mirror queue drops the oldest record instead of blocking or raising")
    client = OverridesClient(
        base_url="http://fake", bot_registration_key="k", enabled=True, start_mirror_thread=False,
    )
    client.http = AlwaysFailsSession()
    # Fill the queue to its cap directly, then push one more.
    while True:
        try:
            client._mirror_queue.put_nowait({"decision_id": "filler"})
        except queue.Full:
            break

    try:
        client.post_ledger_mirror({"decision_id": "newest"})
        raised = False
    except Exception:
        raised = True
    check("post_ledger_mirror never raises even when the queue is completely full", not raised)

    drained = []
    try:
        while True:
            drained.append(client._mirror_queue.get_nowait()["decision_id"])
    except queue.Empty:
        pass
    check("the newest record survives even though the queue was full", "newest" in drained, drained[-5:])


def scenario_mirror_worker_actually_delivers_when_reachable() -> None:
    print("\n5. End-to-end: a reachable Worker actually receives a mirrored record via the background thread")
    client = OverridesClient(
        base_url="http://fake", bot_registration_key="k", enabled=True, start_mirror_thread=True,
    )
    session = RecordingSession()
    client.http = session

    client.post_ledger_mirror({"decision_id": "d-delivered"})
    client._mirror_queue.join()  # wait for the one real background worker thread to drain it

    check(
        "the background worker thread actually POSTs the queued record",
        len(session.post_calls) == 1 and session.post_calls[0][1]["decision_id"] == "d-delivered",
        session.post_calls,
    )


def scenario_slow_5xx_responses_trip_the_circuit_reproducing_owens_review() -> None:
    print("\n6. Owen's #92 review repro: a Worker returning slow HTTP 503s (never an exception) must still trip the circuit")
    # Exactly Owen's repro shape: 22 accounts, 3 GETs per account, every one
    # a 503 after 4.9 simulated seconds. Before the fix, `_get` called
    # `_note_success()` on any non-exception response, so this ran all 66
    # requests (323.4s of simulated time) with the circuit still closed.
    clock = FakeClock()
    session = SlowServerErrorSession(clock, delay_s=4.9, status_code=503)
    client = OverridesClient(
        base_url="http://fake", bot_registration_key="k",
        enabled=True, failure_circuit_threshold=3, circuit_cooldown_s=60.0,
        monotonic=clock, start_mirror_thread=False,
    )
    client.http = session

    for _ in range(22):
        client.fetch_override("some_account")
        client.fetch_kill_switch()
        client.fetch_freeze("some_account")

    check(
        "a 503 is classified as a failure by the caller, so the circuit trips well before 66 requests",
        session.get_calls < 10,
        f"real GET attempts: {session.get_calls} (must not reach 66)",
    )
    check(
        "simulated elapsed time stays far under the runner's 300s cadence",
        clock.now < 60.0,
        f"simulated time: {clock.now:.1f}s",
    )
    check("circuit is open: fetch_override degrades to None", client.fetch_override("x") is None)


def scenario_malformed_200_payload_counts_as_a_failure() -> None:
    print("\n7. A 200 with a malformed/missing-field body is a failure, not a silent success")
    clock = FakeClock()
    responses = [
        _Resp(200, payload={"not_enabled": "nope"}),  # missing/wrong-typed 'enabled' field
        _Resp(200, payload="not-a-dict", json_error=False),  # kill-switch payload not even a dict shape
        _Resp(200, json_error=True),  # 200 but body isn't valid JSON at all
    ]
    session = ScriptedSession(clock, responses)
    client = OverridesClient(
        base_url="http://fake", bot_registration_key="k",
        enabled=True, failure_circuit_threshold=3, circuit_cooldown_s=60.0,
        monotonic=clock, start_mirror_thread=False,
    )
    client.http = session

    for _ in range(3):
        result = client.fetch_kill_switch()
        check("a malformed 200 body degrades to None (fail-closed), never a silently-wrong bool", result is None, result)

    check(
        "three malformed-but-200 responses in a row still trip the circuit",
        client._circuit_is_open(),
        "circuit should be open after 3 consecutive classification failures",
    )


def scenario_control_read_budget_bounds_cumulative_time_without_consecutive_failures() -> None:
    print("\n8. Control-read time budget: bounds cumulative real request time even when failures never string together")
    # Alternate one real success with one slow failure, over and over --
    # `_consecutive_failures` resets to 0 on every success, so the
    # consecutive-failure circuit alone would never trip here. The
    # wall-clock budget must still cut this off well under the runner's
    # cadence.
    clock = FakeClock()

    class AlternatingSlowSession:
        def __init__(self):
            self.get_calls = 0

        def get(self, *a: Any, **kw: Any):
            self.get_calls += 1
            clock.advance(4.9)
            if self.get_calls % 2 == 1:
                return _Resp(200, payload={"enabled": False})
            return _Resp(503)

    session = AlternatingSlowSession()
    client = OverridesClient(
        base_url="http://fake", bot_registration_key="k",
        enabled=True, failure_circuit_threshold=3, circuit_cooldown_s=60.0,
        control_read_budget_s=30.0, monotonic=clock, start_mirror_thread=False,
    )
    client.http = session
    client.begin_cycle()

    for _ in range(66):
        client.fetch_kill_switch()

    check(
        "the circuit itself never opens (failures never string together 3 in a row)",
        not client._circuit_is_open(),
    )
    check(
        "the cycle budget still bounds real requests to a handful, not 66",
        session.get_calls <= 8,
        f"real GET attempts: {session.get_calls}",
    )
    check(
        "cumulative simulated time stays bounded near the configured budget",
        clock.now < 45.0,
        f"simulated time: {clock.now:.1f}s",
    )

    # A new cycle gets a fresh budget.
    session2_calls_before = session.get_calls
    client.begin_cycle()
    client.fetch_kill_switch()
    check(
        "begin_cycle() resets the budget so the next cycle can make real requests again",
        session.get_calls == session2_calls_before + 1,
    )


def main() -> int:
    scenario_circuit_bounds_repeated_failures_across_many_accounts()
    scenario_disabled_client_makes_zero_network_calls()
    scenario_ledger_mirror_never_blocks_the_caller()
    scenario_full_mirror_queue_drops_oldest_rather_than_blocking()
    scenario_mirror_worker_actually_delivers_when_reachable()
    scenario_slow_5xx_responses_trip_the_circuit_reproducing_owens_review()
    scenario_malformed_200_payload_counts_as_a_failure()
    scenario_control_read_budget_bounds_cumulative_time_without_consecutive_failures()

    print()
    print("=" * 66)
    print(f"{passed} passed, {failed} failed")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
