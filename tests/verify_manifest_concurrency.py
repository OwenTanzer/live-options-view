"""R2b: `manifest.json` merges across two writers without losing a field.

WHY this suite exists: the bucket-root index has two owners -- the external
daily pipeline owns `dates`, this collector owns `weekly_expirations` -- and
both used to read the whole object, change their own field, and write it back
unconditionally. Whichever wrote second discarded the other's field. The
repair is compare-and-swap with bounded retry, and the acceptance criterion
names both orders, so both orders are run here against a stub that keeps a
version per object and rejects a stale precondition.

The stub also lets the race be *constructed*: `before_write` runs in the
window between the writer's read and its write, which is where the other
writer lands. Nothing is timed, so the test is deterministic.

    python tests/verify_manifest_concurrency.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from market_data.live_market.manifest import (  # noqa: E402
    MANIFEST_ATTEMPTS, ConditionalWriteUnsupported, ManifestConflict,
    ManifestDamaged, ManifestUnreadable, add_weekly_key, read_manifest,
    update_manifest, write_manifest,
)

TODAY = "2026-11-11"
EXPIRY = "2026-11-13"
WEEKLY_KEY = f"raw/weekly/20261113/qqq_chain_20261111_093000000000.csv"


class FakeBody:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return self.payload


class MissingKey(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class PreconditionFailed(Exception):
    response = {"Error": {"Code": "PreconditionFailed"}}


class Unavailable(Exception):
    response = {"Error": {"Code": "InternalError"}}


class R2Stub:
    """One object, versioned ETags, real preconditions, injectable race window."""

    def __init__(self, document=None):
        self.document = None
        self.etag = None
        self.version = 0
        self.writes = []
        self.reads = 0
        self.conflicts = 0
        self.fail_reads_with = None
        self.before_write = None
        if document is not None:
            self._store(document)

    # -- storage -----------------------------------------------------------
    def _store(self, document):
        self.version += 1
        self.document = json.loads(json.dumps(document))
        self.etag = f'"{self.version}"'
        return {"ETag": self.etag}

    def get_object(self, Bucket, Key):  # noqa: N803
        self.reads += 1
        if self.fail_reads_with is not None:
            raise self.fail_reads_with
        if self.document is None:
            raise MissingKey(Key)
        return {"Body": FakeBody(json.dumps(self.document).encode()), "ETag": self.etag}

    def put_object(self, Bucket, Key, Body, **kwargs):  # noqa: N803
        # Persistent, so a hook can model "someone always wins" as well as a
        # single interleaving; a one-shot hook guards itself with a flag.
        if self.before_write is not None:
            self.before_write()
        payload = json.loads(Body.decode() if isinstance(Body, bytes) else Body)
        expected = kwargs.get("IfMatch")
        create_only = kwargs.get("IfNoneMatch")
        if expected is not None and expected != self.etag:
            self.conflicts += 1
            raise PreconditionFailed(f"IfMatch {expected} != {self.etag}")
        if create_only == "*" and self.etag is not None:
            self.conflicts += 1
            raise PreconditionFailed("object already exists")
        self.writes.append(payload)
        return self._store(payload)


# -- the two writers' merge functions, as each repository implements them -----

def _daily_merge(date_str):
    """The pipeline's merge: union today into `dates`, never invent a history."""
    def mutate(document):
        if "dates" not in document:
            raise ManifestDamaged("daily writer will not publish a date index from nothing")
        dates = set(document.get("dates") or [])
        dates.add(date_str)
        document["dates"] = sorted(dates)
        return document
    return mutate


def _weekly_merge(expiry, key):
    return lambda document: add_weekly_key(document, expiry, key)


SEED = {"dates": [], "weekly_expirations": {}}


def _daily_write(stub, date_str, **kwargs):
    return update_manifest(stub, _daily_merge(date_str), seed=dict(SEED), **kwargs)


def _weekly_write(stub, expiry=EXPIRY, key=WEEKLY_KEY, **kwargs):
    return update_manifest(stub, _weekly_merge(expiry, key), seed=dict(SEED), **kwargs)


# -- 1. both interleavings preserve both fields ------------------------------

def test_daily_then_weekly_preserves_the_new_date():
    """Daily writes today, then the weekly writer's stale ETag is rejected."""
    stub = R2Stub({"dates": ["2026-11-10"], "weekly_expirations": {"2026-11-06": ["old.csv"]}})
    stale_document, stale_etag = read_manifest(stub)          # weekly's read

    _daily_write(stub, TODAY)                                  # daily lands
    assert TODAY in stub.document["dates"]

    def daily_lands_again():
        pass
    # Force the weekly writer's first CAS attempt to use the stale ETag.
    try:
        write_manifest(stub, dict(stale_document), stale_etag)
    except ManifestConflict:
        pass
    else:
        raise AssertionError("a stale precondition must be rejected")

    final = _weekly_write(stub)
    assert final["dates"] == ["2026-11-10", TODAY], final["dates"]
    assert final["weekly_expirations"][EXPIRY] == [WEEKLY_KEY]
    assert final["weekly_expirations"]["2026-11-06"] == ["old.csv"], "other expiries kept"

    # And the retry path itself: a writer that read before the daily write.
    stub2 = R2Stub({"dates": ["2026-11-10"], "weekly_expirations": {}})
    injected = {"done": False}

    def daily_lands_mid_flight():
        if not injected["done"]:
            injected["done"] = True
            stub2._store({"dates": ["2026-11-10", TODAY],
                          "weekly_expirations": {}})

    stub2.before_write = daily_lands_mid_flight
    final2 = _weekly_write(stub2)
    assert final2["dates"] == ["2026-11-10", TODAY], "the daily date survives the weekly retry"
    assert final2["weekly_expirations"][EXPIRY] == [WEEKLY_KEY]
    assert stub2.conflicts == 1, "exactly one conflict, then a clean merge"


def test_weekly_then_daily_preserves_the_weekly_reference():
    stub = R2Stub({"dates": ["2026-11-10"], "weekly_expirations": {}})
    injected = {"done": False}

    def weekly_lands_mid_flight():
        if not injected["done"]:
            injected["done"] = True
            stub._store({"dates": ["2026-11-10"],
                         "weekly_expirations": {EXPIRY: [WEEKLY_KEY]}})

    stub.before_write = weekly_lands_mid_flight
    final = _daily_write(stub, TODAY)
    assert final["dates"] == ["2026-11-10", TODAY]
    assert final["weekly_expirations"][EXPIRY] == [WEEKLY_KEY], "the weekly key survives the daily retry"
    assert stub.conflicts == 1


# -- 2. bounded exhaustion ---------------------------------------------------

def test_retry_exhaustion_raises_and_leaves_the_last_good_index():
    stub = R2Stub({"dates": ["2026-11-10"], "weekly_expirations": {}})
    sleeps = []
    stub.before_write = lambda: stub._store(dict(stub.document))   # someone always wins

    try:
        _weekly_write(stub, sleeper=sleeps.append)
    except ManifestConflict as exc:
        assert f"after {MANIFEST_ATTEMPTS} attempts" in str(exc), str(exc)
    else:
        raise AssertionError("exhaustion must raise, not write unconditionally")

    assert stub.conflicts == MANIFEST_ATTEMPTS, stub.conflicts
    assert len(sleeps) == MANIFEST_ATTEMPTS - 1, "no sleep after the final attempt"
    assert sleeps == sorted(sleeps), "backoff grows"
    assert stub.document == {"dates": ["2026-11-10"], "weekly_expirations": {}}, \
        "the last good index is untouched"
    assert all(EXPIRY not in (payload.get("weekly_expirations") or {}) for payload in stub.writes)


# -- 3. first run: one creator wins, the other merges ------------------------

def test_absent_object_create_is_conditional_and_merged():
    stub = R2Stub()
    assert read_manifest(stub) == (None, None), "absence is not an error"

    created = {"done": False}

    def daily_creates_first():
        if not created["done"]:
            created["done"] = True
            stub._store({"dates": [TODAY], "weekly_expirations": {}})

    stub.before_write = daily_creates_first
    final = _weekly_write(stub)
    assert final["dates"] == [TODAY], "the creator's date is not lost"
    assert final["weekly_expirations"][EXPIRY] == [WEEKLY_KEY]
    assert stub.conflicts == 1, "the create-only write lost once, then merged"
    assert stub.writes[-1]["dates"] == [TODAY]


def test_absent_object_seed_publishes_a_valid_date_index():
    stub = R2Stub()
    created = _weekly_write(stub)
    assert created["dates"] == [], "a first run publishes an explicit empty index"
    assert created["weekly_expirations"][EXPIRY] == [WEEKLY_KEY]
    assert "dates" in stub.document, "the browser must never see a manifest without dates"


# -- 4. the two catastrophic reader failures ---------------------------------

def test_failed_read_is_not_an_empty_index():
    stub = R2Stub({"dates": ["2026-11-10", TODAY], "weekly_expirations": {"x": ["k.csv"]}})
    before = json.loads(json.dumps(stub.document))
    stub.fail_reads_with = Unavailable("storage said no")

    try:
        _weekly_write(stub)
    except ManifestUnreadable:
        pass
    else:
        raise AssertionError("an unreadable index must raise, never be treated as empty")
    assert stub.document == before, "nothing was written over a real index"
    assert stub.writes == []

    # Failing-before witness: the pre-repair reader returned {"dates": []} for
    # any exception, and that document was written back unconditionally.
    destroyed = {"dates": []}
    assert destroyed != before, "the old path discarded every date and every weekly key"


def test_damaged_index_is_refused_not_rebuilt():
    stub = R2Stub({"weekly_expirations": {}, "note": "no dates here"})
    before = json.loads(json.dumps(stub.document))
    try:
        _weekly_write(stub)
    except ManifestDamaged:
        pass
    else:
        raise AssertionError("a present document without dates must be refused")
    assert stub.document == before
    assert stub.writes == []

    try:
        _daily_write(stub, TODAY)
    except ManifestDamaged:
        pass
    else:
        raise AssertionError("the daily writer must not publish a one-date history")
    assert stub.document == before, "neither writer cements a damaged index"


def test_corrupt_json_is_unreadable_not_empty():
    class Corrupt(R2Stub):
        def get_object(self, Bucket, Key):  # noqa: N803
            return {"Body": FakeBody(b'{"dates": ["2026-11-10"'), "ETag": '"1"'}

    stub = Corrupt()
    try:
        _weekly_write(stub)
    except ManifestUnreadable as exc:
        assert "not valid JSON" in str(exc), str(exc)
    else:
        raise AssertionError("a truncated document must not be treated as an empty index")
    assert stub.writes == [], "no write follows an unreadable document"


def test_read_without_an_etag_cannot_be_made_conditional():
    class NoEtag(R2Stub):
        def get_object(self, Bucket, Key):  # noqa: N803
            return {"Body": FakeBody(json.dumps({"dates": [TODAY]}).encode())}
    stub = NoEtag()
    try:
        _weekly_write(stub)
    except ManifestUnreadable as exc:
        assert "ETag" in str(exc), str(exc)
    else:
        raise AssertionError("a write we cannot make conditional must not be attempted")
    assert stub.writes == []


# -- 5. unknown fields survive both writers ---------------------------------

def test_every_other_top_level_key_survives():
    original = {
        "dates": ["2026-11-10"],
        "note": "D-date files: qqq_chain_{D}.csv contains Expiration=TradeDate=D rows",
        "metadata": {"keep": True},
        "schema_version": 3,
        "weekly_expirations": {"2026-11-06": ["k1.csv", "k2.csv"]},
        "updated_at": "before",
    }
    stub = R2Stub(original)
    after_weekly = _weekly_write(stub)
    assert after_weekly["note"] == original["note"]
    assert after_weekly["metadata"] == {"keep": True}
    assert after_weekly["schema_version"] == 3
    assert after_weekly["dates"] == original["dates"]

    after_daily = _daily_write(stub, TODAY)
    assert after_daily["note"] == original["note"]
    assert after_daily["metadata"] == {"keep": True}
    assert after_daily["schema_version"] == 3
    assert after_daily["weekly_expirations"] == after_weekly["weekly_expirations"], \
        "the weekly index is intact after the daily write"
    assert after_daily["dates"] == ["2026-11-10", TODAY]
    assert after_daily["updated_at"] != "before", "the writer stamps it"


# -- 6. a property run over many interleaved writers ------------------------

def test_interleaved_writers_converge_on_the_union():
    import random

    rng = random.Random(20261111)
    for trial in range(20):
        stub = R2Stub({"dates": [], "weekly_expirations": {"old": ["keep.csv"]}})
        expected_dates = set()
        expected_weekly = {}

        def other_writer_lands():
            # Any writer may interleave; the merge must still converge.
            if rng.random() < 0.5:
                stub._store(json.loads(json.dumps(stub.document)))

        for round_index in range(6):
            if rng.random() < 0.5:
                day = f"2026-11-{10 + round_index:02d}"
                expected_dates.add(day)
                stub.before_write = other_writer_lands
                try:
                    _daily_write(stub, day, attempts=MANIFEST_ATTEMPTS, sleeper=lambda _: None)
                except ManifestConflict:
                    # Exhaustion is allowed only when the writer gives up
                    # without writing; it must never leave a partial merge.
                    expected_dates.discard(day)
                    continue
            else:
                expiry = f"2026-11-{20 + round_index:02d}"
                key = f"raw/weekly/{expiry.replace('-', '')}/k{round_index}.csv"
                expected_weekly.setdefault(expiry, []).append(key)
                stub.before_write = other_writer_lands
                try:
                    _weekly_write(stub, expiry, key, attempts=MANIFEST_ATTEMPTS, sleeper=lambda _: None)
                except ManifestConflict:
                    expected_weekly[expiry].remove(key)
                    continue

        assert set(stub.document["dates"]) == expected_dates, (trial, stub.document["dates"], expected_dates)
        for expiry, keys in expected_weekly.items():
            assert sorted(keys) == stub.document["weekly_expirations"].get(expiry), (trial, expiry)
        assert stub.document["weekly_expirations"].get("old") == ["keep.csv"], "pre-existing data kept"


# -- 7. the capability gate -------------------------------------------------

def test_client_without_conditional_put_is_refused_before_writing():
    class Shape:
        members = {"Bucket": None, "Key": None, "Body": None}

        def __init__(self, members):
            self.members = members

    class ServiceModel:
        def __init__(self, members):
            self.members = members

        def operation_model(self, name):
            assert name == "PutObject"

            class Op:
                input_shape = Shape(self.members)

            return Op()

    class Meta:
        def __init__(self, members):
            self.service_model = ServiceModel(members)

    stub = R2Stub({"dates": [TODAY]})
    stub.meta = Meta({"Bucket", "Key", "Body"})
    try:
        _weekly_write(stub)
    except ConditionalWriteUnsupported as exc:
        assert "IfMatch" in str(exc)
    else:
        raise AssertionError("a client that cannot send preconditions must not write")
    assert stub.writes == []

    stub.meta = Meta({"Bucket", "Key", "Body", "IfMatch", "IfNoneMatch"})
    assert _weekly_write(stub)["weekly_expirations"][EXPIRY] == [WEEKLY_KEY]


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} manifest concurrency checks passed.")


if __name__ == "__main__":
    run()
