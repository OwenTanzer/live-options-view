"""G7: a local pilot run through the real CLI and publisher, against disposable local stores.

    python tests/news_g7_local_pilot.py OUT_DIR

Runs pilot cycles with the shipped (inactive) news-pilot.json on fixture feeds and LocalObjectStore
directories under OUT_DIR, injecting faults into the local public store only:

  S1 published        two ordinary cycles
  S2 upload-timeout   a cycle whose first page PUT times out; the request is held (it may still land)
  S3 cap-paused       a cycle with the page cap lowered to what is already reserved: paused
  S4 late-landing     the held PUT lands in the quarantined namespace, named by no index
  S5 index-ambiguous  a cycle whose index write lands but its answer is lost
  S6 settled          the next ordinary cycle observes and settles it
  S7 withdrawn        a cycle with another timed-out page PUT, then publish-stop + publish-withdraw
  S8 reappeared       that PUT lands after the withdrawal: the page reappears, named by no index
  S9 withdrawn-again  publish-withdraw again deletes it; the quarantined entries keep their reservation

After each stage the public directory is copied to OUT_DIR/stages/<stage>/ and the stage's expected
reader outcome is written to OUT_DIR/stages.json for tests/news_pilot_local_browser.test.js. The
publisher-side invariants are asserted here (status problems, quarantine, reservation <= cap, listings).

Everything is local: fixture feeds, temporary directories, no provider, bucket, credential or network.
The fault injection models a timed-out create-if-absent and a lost CAS answer; it is not evidence about
R2 or boto3 (A1-A8).
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from market_data.news import publish as pub  # noqa: E402
from market_data.news.__main__ import DEFAULT_FIXTURES, fixed_or_real_clock, fixture_fetcher, main  # noqa: E402
from market_data.news.archive import CasOutcome, LocalObjectStore  # noqa: E402

CONFIG = ROOT / "market_data/news/deploy/news-pilot.json"


class Faults:
    """Faults on the public LocalObjectStore only (matched by root), installed for the whole run."""

    def __init__(self, public_root: Path):
        self.public_root = Path(public_root).resolve()
        self.hold_next_page = False
        self.held: list[tuple] = []
        self.lose_index_answer = False
        self._put_if_absent = LocalObjectStore.put_if_absent
        self._put_if_match = LocalObjectStore.put_if_match

    def _public(self, store) -> bool:
        return Path(store.root).resolve() == self.public_root

    def install(self):
        faults = self

        def put_if_absent(store, key, data, content_type="application/octet-stream", cache_control=None):
            if faults._public(store) and faults.hold_next_page and key.startswith(pub.PAGES_PREFIX):
                faults.hold_next_page = False
                faults.held.append((store, key, data, content_type, cache_control))
                raise TimeoutError(f"injected: PUT {key} timed out; the request may still land")
            return faults._put_if_absent(store, key, data, content_type=content_type, cache_control=cache_control)

        def put_if_match(store, key, data, etag, content_type="application/octet-stream", cache_control=None,
                         write_id=None):
            out = faults._put_if_match(store, key, data, etag, content_type, cache_control=cache_control,
                                       write_id=write_id)
            if faults._public(store) and faults.lose_index_answer and key == pub.PUBLIC_INDEX and out.applied:
                faults.lose_index_answer = False
                return CasOutcome("ambiguous", detail="injected: applied, answer lost")
            return out

        LocalObjectStore.put_if_absent = put_if_absent
        LocalObjectStore.put_if_match = put_if_match

    def land(self) -> tuple[str, bool]:
        store, key, data, content_type, cache_control = self.held.pop(0)
        return key, self._put_if_absent(store, key, data, content_type=content_type, cache_control=cache_control)


def run_cli(*argv) -> tuple[int | str, dict | None, str]:
    """main() in process. Returns (exit code or exception name, parsed stdout JSON or None, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
    except Exception as exc:  # an exception the CLI does not map to an exit code
        return type(exc).__name__, None, f"{type(exc).__name__}: {exc}"
    text = out.getvalue().strip()
    return code, (json.loads(text) if text.startswith("{") else None), err.getvalue()


def main_run(out_dir: Path) -> list[dict]:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    private, public, spool = out_dir / "private", out_dir / "public", out_dir / "spool"
    stages_dir = out_dir / "stages"
    stages_dir.mkdir(parents=True)
    base = ["--config", str(CONFIG), "--private", str(private), "--public", str(public)]
    faults = Faults(public)
    faults.install()
    stages, log = [], []

    def cycle(clock):
        code, out, err = run_cli("pilot-cycle", *base, "--spool", str(spool), "--clock", clock)
        log.append({"cmd": f"pilot-cycle --clock {clock}", "exit": code, "stderr": err.strip()[-300:]})
        return code, out, err

    def store_status(clock):
        cfg = pub.load_pilot_config(CONFIG)
        return pub.status(LocalObjectStore(private), pub.PublicViewStore(LocalObjectStore(public)),
                          fixed_or_real_clock(clock), policy=pub.policy_from(cfg))

    def listed():
        return pub.PublicViewStore(LocalObjectStore(public)).pages_by_incarnation()

    def live():
        raw = LocalObjectStore(public).get(pub.PUBLIC_INDEX)
        return json.loads(raw) if raw else None

    def snap(name, expect, note, forbidden=(), st=None):
        dest = stages_dir / name
        shutil.copytree(public, dest)
        idx = live()
        stages.append({"stage": name, "expect": expect, "note": note,
                       "live_state": idx.get("state") if idx else None, "live_gen": (idx or {}).get("gen"),
                       "first_page": (idx or {}).get("views", [{}])[0].get("first_page") if idx and idx.get("views") else None,
                       "forbidden_prefixes": sorted(forbidden),
                       "problems": (st or {}).get("problems"),
                       "reserved_pages": (st or {}).get("reserved_pages"), "public_pages": (st or {}).get("public_pages")})

    def prefix(inc):
        return f"{pub.PAGES_PREFIX}{inc}/"

    # S1: two ordinary cycles.
    for clock in ("2026-11-09T10:05:00Z", "2026-11-09T10:20:00Z"):
        code, out, err = cycle(clock)
        assert code == 0, (clock, code, err)
    s1 = live()
    assert s1["state"] == "published"
    st = store_status("2026-11-09T10:21:00Z")
    snap("S1-published", "ready", "two ordinary cycles", st=st)

    # S2: the first page PUT of the next incarnation times out and is held.
    faults.hold_next_page = True
    code, out, err = cycle("2026-11-09T10:35:00Z")
    assert code == "TimeoutError", (code, err)
    assert live()["gen"] == s1["gen"], "readers keep the last good generation"
    st = store_status("2026-11-09T10:36:00Z")
    quarantined = sorted(i for i, g in st["generations"].items() if g["class"] == "quarantined")
    assert len(quarantined) == 1, st["generations"]
    q1 = quarantined[0]
    assert "page_uploads_unsettled" in st["problems"], st["problems"]
    assert st["generations"][q1]["pages"] == 0 and st["generations"][q1]["reserved"] > 0
    snap("S2-upload-timeout", "ready", f"first page PUT into {q1} timed out and is held", {prefix(q1)}, st)

    # S3: cap lowered to what is already reserved: the next cycle pauses; nothing is evicted.
    cfg = pub.load_pilot_config(CONFIG)
    cfg.raw["generations"]["page_cap"] = st["reserved_pages"]
    priv, publ = LocalObjectStore(private), pub.PublicViewStore(LocalObjectStore(public))
    try:
        pub.pilot_cycle(priv, publ, str(spool), fixture_fetcher(Path(DEFAULT_FIXTURES)),
                        fixed_or_real_clock("2026-11-09T10:50:00Z"), cfg)
        raise AssertionError("admission over the cap was not paused")
    except pub.PublishPaused as exc:
        log.append({"cmd": f"pilot_cycle page_cap={st['reserved_pages']} --clock 2026-11-09T10:50:00Z",
                    "exit": "PublishPaused", "stderr": str(exc)[:300]})
    st3 = pub.status(priv, publ, fixed_or_real_clock("2026-11-09T10:51:00Z"), policy=pub.policy_from(cfg))
    assert "publication_paused" in st3["problems"], st3["problems"]
    assert st3["generations"][q1]["class"] == "quarantined"
    assert st3["reserved_pages"] == st["reserved_pages"], "the quarantined reservation was not released"
    assert live()["gen"] == s1["gen"]
    snap("S3-cap-paused", "ready", f"page_cap={st['reserved_pages']}: admission paused", {prefix(q1)}, st3)

    # S4: the held PUT lands now, in the quarantined namespace.
    key, created = faults.land()
    assert created, key
    pages = listed()
    assert pages.get(q1) == [key], pages.get(q1)
    st4 = pub.status(priv, publ, fixed_or_real_clock("2026-11-09T10:52:00Z"), policy=pub.policy_from(cfg))
    assert st4["public_pages"] <= st4["reserved_pages"] <= st["reserved_pages"], (st4["public_pages"], st4["reserved_pages"])
    assert live()["gen"] == s1["gen"]
    snap("S4-late-landing", "ready", f"held PUT landed: {key}", {prefix(q1)}, st4)

    # S5: the index write lands but its answer is lost.
    faults.lose_index_answer = True
    code, out, err = cycle("2026-11-09T11:05:00Z")
    assert code == 2 and "has an unknown outcome" in err, (code, err)   # the CLI maps it to exit 2
    s5 = live()
    assert s5["gen"] != s1["gen"], "the commit landed: readers get the new incarnation"
    st5 = store_status("2026-11-09T11:06:00Z")
    assert "open_index_commits" in st5["problems"], st5["problems"]
    snap("S5-index-ambiguous", "ready", "index write applied, answer lost: commit open", {prefix(q1)}, st5)

    # S6: the next ordinary cycle observes and settles it.
    code, out, err = cycle("2026-11-09T11:20:00Z")
    assert code == 0, (code, err)
    st6 = store_status("2026-11-09T11:21:00Z")
    assert st6["open_commits"] == {}, st6["open_commits"]
    assert st6["generations"][q1]["class"] == "quarantined"
    snap("S6-settled", "ready", "next cycle settled the open commit", {prefix(q1)}, st6)

    # S7: another timed-out page PUT, then stop + withdraw.
    faults.hold_next_page = True
    code, out, err = cycle("2026-11-09T11:35:00Z")
    assert code == "TimeoutError", (code, err)
    code, out, err = run_cli("publish-stop", *base, "--reason", "G7 local withdrawal check", "--clock", "2026-11-09T11:40:00Z")
    log.append({"cmd": "publish-stop", "exit": code, "stderr": err.strip()[-300:]})
    assert code == 0, err
    code, out, err = run_cli("publish-withdraw", *base, "--clock", "2026-11-09T11:41:00Z")
    log.append({"cmd": "publish-withdraw", "exit": code, "stderr": err.strip()[-300:], "out": out})
    assert code == 0 and out["withdrawal_unconfirmed"] is False, (code, out, err)
    assert len(out["uploads_unsettled"]) == 2 and q1 in out["uploads_unsettled"], out["uploads_unsettled"]
    q2 = [i for i in out["uploads_unsettled"] if i != q1][0]
    assert listed() == {}, "every page deleted"
    assert live()["state"] == "withdrawn"
    st7 = store_status("2026-11-09T11:42:00Z")
    assert "page_uploads_unsettled" in st7["problems"]
    snap("S7-withdrawn", "withdrawn", f"withdrawn; uploads unsettled: {out['uploads_unsettled']}",
         {prefix(q1), prefix(q2)}, st7)

    # S8: the PUT sent before the withdrawal lands after it.
    key2, created = faults.land()
    assert created and key2.startswith(prefix(q2)), key2
    assert live()["state"] == "withdrawn", "the tombstone still holds the index"
    st8 = store_status("2026-11-09T11:43:00Z")
    assert st8["public_pages"] == 1 and st8["generations"][q2]["class"] == "retiring"
    snap("S8-reappeared", "withdrawn", f"page reappeared after withdrawal: {key2}", {prefix(q1), prefix(q2)}, st8)

    # S9: withdraw again deletes it; the entries and their reservations stay.
    code, out, err = run_cli("publish-withdraw", *base, "--clock", "2026-11-09T11:44:00Z")
    log.append({"cmd": "publish-withdraw (again)", "exit": code, "stderr": err.strip()[-300:], "out": out})
    assert code == 0 and out["deleted"] == 1, (code, out, err)
    assert listed() == {}
    st9 = store_status("2026-11-09T11:45:00Z")
    assert set(st9["generations"]) >= {q1, q2} or st9["reserved_pages"] > 0, st9["generations"]
    assert st9["reserved_pages"] > 0, "quarantined reservations are kept"
    snap("S9-withdrawn-again", "withdrawn", "second withdraw deleted the reappeared page", {prefix(q1), prefix(q2)}, st9)

    (out_dir / "stages.json").write_text(json.dumps(stages, indent=1), encoding="utf-8")
    (out_dir / "commands.json").write_text(json.dumps(log, indent=1, default=str), encoding="utf-8")
    return stages


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    for s in main_run(Path(sys.argv[1])):
        print(f"ok - {s['stage']}: {s['expect']} ({s['note']}); problems={s['problems']} "
              f"reserved={s['reserved_pages']} public={s['public_pages']}")
    print("news G7 local pilot scenarios passed")
