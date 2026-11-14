"""P1 research scenarios on disposable local stores (shared by tests/test_news_research.py and
tests/news_research_browser.test.js).

    python tests/news_research_scenarios.py OUT_DIR    writes projections + stages.json for the browser test

prepared   the six records A-F (tests/fixtures/news/research/positioning_p1_prepared.json), imported
           with a TEST clock of 2026-11-13T18:00:00Z (commit 18:00:05Z). That clock is a fixture
           value in a disposable store, not a claim about when any real annotation was committed.
synthetic  a SYNTHETIC timeline for the gates the prepared records cannot show on their own:
             R1  capture S-site rev 1 (published 11-01 12:00Z, accessed 11-02 09:00Z); run at 09:30,
                 commit DELAYED: settled at 11:00
             R2  annotation S-a v1 on rev 1 at 12:00 (contemporaneous hypothesis, horizon 11-05)
             R3  observation annotation S-obs at 13:00 on a COLLECTED revision (observed 08:00, archived
                 08:00:05); commit delayed, settled 15:00; its hypothesis is retrospective
             R4  annotation S-a v2 (a revision of the same lineage) on 11-03 at 10:00
             R5  capture S-site rev 2 (updated 07:30, accessed 07:45) on 11-04 at 08:00
             R6  annotation S-b on rev 2 at 09:00
             R7  a 13F filing capture: EDGAR acceptance 11-16 21:05:12Z, our access 11-17 14:00Z,
                 capture commit 11-17 14:30:05Z (the due date 11-16 is never availability)
Nothing here fetches anything; every store is a temporary directory.
"""
from __future__ import annotations

import copy
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from market_data.news import research as R  # noqa: E402
from market_data.news.archive import LocalObjectStore  # noqa: E402

PREPARED = ROOT / "tests/fixtures/news/research/positioning_p1_prepared.json"
SYN_LABEL = "SYNTHETIC research records (P1 test timeline): not real parties or events"


def at(s: str):
    t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return lambda: t


def load_prepared() -> dict:
    return json.loads(PREPARED.read_text(encoding="utf-8"))


def prepared_store(root) -> LocalObjectStore:
    st = LocalObjectStore(root)
    R.import_bundle(st, load_prepared(), at("2026-11-13T18:00:00Z"), commit_clock=at("2026-11-13T18:00:05Z"))
    return st


# ── synthetic timeline ───────────────────────────────────────────────────────

SITE = {"key": "S-site", "party": "Synthetic Wire", "title": "SYNTHETIC: fund reported cutting gross exposure",
        "url": "https://example.test/research/s-site",
        "published": {"at": "2026-11-01T12:00Z", "kind": "publication", "basis": "page metadata"},
        "access": {"result": "read", "accessed_at": "2026-11-02T09:00:00Z", "via": "synthetic", "basis": "test"}}
SITE_V2 = dict(SITE, updated="2026-11-04T07:30Z",
               access={"result": "read", "accessed_at": "2026-11-04T07:45:00Z", "via": "synthetic", "basis": "test"},
               title="SYNTHETIC: fund reported cutting gross exposure (updated: the cut was larger)")
FILING = {"key": "S-13f", "party": "SEC EDGAR (SYNTHETIC filer)", "title": "SYNTHETIC 13F-HR for quarter ended 2026-09-30",
          "url": "https://example.test/edgar/s-13f",
          "published": {"at": "2026-11-16T21:05:12Z", "kind": "edgar_acceptance", "basis": "EDGAR acceptance datetime"},
          "access": {"result": "read", "accessed_at": "2026-11-17T14:00:00Z", "via": "synthetic", "basis": "test"}}


def hop(cap, party="Synthetic Wire", url="https://example.test/research/s-site"):
    return {"party": party, "role": "publisher", "capture": cap, "url": url, "access": {"result": "read"}}


def claim(cid, status="reported", value=-5.0, kind="attributed_report", cap="S-site"):
    return {"id": cid, "evidence_kind": kind, "chain": [hop(cap)], "subject": {"party": "Synthetic Fund LP"},
            "exposure": {"direction": "gross"}, "action": {"kind": "de_gross", "status": status},
            "quantities": [{"value": value, "unit": "pp", "of": "gross exposure / equity (change)",
                            "measured_at": "unknown", "basis": "reported"}]}


def bundle(captures, annotations):
    return {"schema_version": R.BUNDLE_SCHEMA, "label": SYN_LABEL, "captures": captures, "annotations": annotations}


def synthetic_observation() -> dict:
    """One collected revision, as reader.Archive.observations carries it (fields the view uses)."""
    return {"item_id": "obs-item-1", "revision_id": "obs-rev-1", "previous_revision_id": None,
            "source_id": "synthetic_feed", "source_name": "Synthetic Feed", "outlet": "Synthetic Feed",
            "title": "SYNTHETIC collected headline: desk reports covering", "canonical_url": "https://example.test/feed/1",
            "published_at": "2026-11-02T07:50:00Z", "observed_at": "2026-11-02T08:00:00Z",
            "first_seen_at": "2026-11-02T08:00:00Z", "archived_at": "2026-11-02T08:00:05Z", "run_id": "synthetic-run"}


def synthetic_store(root) -> tuple[LocalObjectStore, list[dict], dict]:
    st = LocalObjectStore(root)
    obs = synthetic_observation()
    log = {}
    log["R1"] = R.import_bundle(st, bundle([SITE], []), at("2026-11-02T09:30:00Z"), defer_commit=True)
    log["R1_settled"] = R.settle_research(st, at("2026-11-02T11:00:00Z"))
    s_a_v1 = {"lineage": "S-a", "capture": "S-site", "method": "manual", "version": "syn-1", "claims": [claim("S1")],
              "hypothesis": {"statement": "SYNTHETIC: de-grossing continues into the next weekly report.",
                             "instruments": ["QQQ"], "horizon": {"start": "2026-11-05", "end": "2026-11-12"},
                             "disconfirmation": "SYNTHETIC: the next weekly shows re-grossing."}}
    log["R2"] = R.import_bundle(st, bundle([SITE], [s_a_v1]), at("2026-11-02T12:00:00Z"),
                                commit_clock=at("2026-11-02T12:00:05Z"))
    s_obs = {"lineage": "S-obs", "input_revision_id": obs["revision_id"], "method": "manual", "version": "syn-1",
             "claims": [dict(claim("O1", cap=None), chain=[{"party": "Synthetic Feed", "role": "publisher",
                                                             "url": "https://example.test/feed/1",
                                                             "access": {"result": "read"}}])],
             "hypothesis": {"statement": "SYNTHETIC: the covering was a one-day release.", "instruments": ["SPY"],
                            "horizon": {"start": "2026-11-01", "end": "2026-11-03"},
                            "disconfirmation": "SYNTHETIC: continued net buying next week."}}
    log["R3"] = R.import_bundle(st, bundle([], [s_obs]), at("2026-11-02T13:00:00Z"), defer_commit=True)
    log["R3_settled"] = R.settle_research(st, at("2026-11-02T15:00:00Z"))
    s_a_v2 = copy.deepcopy(s_a_v1)
    s_a_v2["version"] = "syn-2"
    s_a_v2["claims"] = [claim("S1", value=-6.5)]
    log["R4"] = R.import_bundle(st, bundle([SITE], [s_a_v2]), at("2026-11-03T10:00:00Z"),
                                commit_clock=at("2026-11-03T10:00:05Z"))
    log["R5"] = R.import_bundle(st, bundle([SITE_V2], []), at("2026-11-04T08:00:00Z"),
                                commit_clock=at("2026-11-04T08:00:05Z"))
    s_b = {"lineage": "S-b", "capture": "S-site", "method": "manual", "version": "syn-1",
           "claims": [claim("S2", status="contradicted", value=-9.0)]}
    log["R6"] = R.import_bundle(st, bundle([SITE_V2], [s_b]), at("2026-11-04T09:00:00Z"),
                                commit_clock=at("2026-11-04T09:00:05Z"))
    f = {"lineage": "S-13f", "capture": "S-13f", "method": "manual", "version": "syn-1", "claims": [{
        "id": "F1", "evidence_kind": "primary_disclosure",
        "chain": [{"party": "SEC EDGAR", "role": "origin", "capture": "S-13f", "url": "https://example.test/edgar/s-13f",
                   "access": {"result": "read"}}],
        "subject": {"party": "Synthetic Capital LP"}, "exposure": {"direction": "long", "instrument_ids": ["etf.qqq"]},
        "action": {"kind": "disclose_long_positions", "status": "reported"},
        "quantities": [{"value": 1200000, "unit": "shares", "of": "QQQ shares held (long)", "measured_at": "2026-09-30",
                        "basis": "reported"}],
        "dates": [{"kind": "quarter_end", "at": "2026-09-30"}, {"kind": "filing_due", "at": "2026-11-16"}]}]}
    log["R7"] = R.import_bundle(st, bundle([FILING], [f]), at("2026-11-17T14:30:00Z"),
                                commit_clock=at("2026-11-17T14:30:05Z"))
    return st, [obs], log


# ── stages for the rendered check ────────────────────────────────────────────

STAGES = [
    # name, store, mode, cutoff, what the page must show (substrings), what it must not show
    ("prepared-research", "prepared", "research", "2026-11-13T18:01:00Z",
     ["RESEARCH", "attributed report", "thesis assertion", "RETROSPECTIVE hypothesis", "CONTEMPORANEOUS hypothesis",
      "net exposure / equity", "bid_deadline", "not evidence"], ["made after cutoff"]),
    ("prepared-before-commit", "prepared", "research", "2026-11-13T17:59:00Z", ["No research records visible"], []),
    ("prepared-publication", "prepared", "publication", "2026-11-08T00:00:00Z",
     ["RETROSPECTIVE publication-time view", "made after cutoff", "Walleye"], ["Briefs"]),
    ("synthetic-delayed-commit", "synthetic", "research", "2026-11-02T10:00:00Z", ["No research records visible"], []),
    ("synthetic-capture-only", "synthetic", "research", "2026-11-02T11:30:00Z",
     ["SYNTHETIC", "capture committed 2026-11-02T11:00:00Z", "no annotation visible"], []),
    ("synthetic-annotation-v2", "synthetic", "research", "2026-11-03T12:00:00Z",
     ["revision 2 of 2", "syn-2", "CONTEMPORANEOUS hypothesis"], []),
    ("synthetic-new-revision", "synthetic", "research", "2026-11-04T09:30:00Z",
     ["capture revision 2", "contradicted", "on an earlier revision: S-a"], []),
    ("synthetic-archive", "synthetic", "archive", "2026-11-02T15:30:00Z",
     ["collected", "withheld"], ["RETROSPECTIVE hypothesis"]),
    ("synthetic-edgar", "synthetic", "research", "2026-11-17T15:00:00Z",
     ["EDGAR acceptance 2026-11-16T21:05:12Z", "accessed 2026-11-17T14:00:00Z", "capture committed 2026-11-17T14:30:05Z",
      "2026-09-30"], []),
]


def build(out_dir: Path) -> list[dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stores = {"prepared": (prepared_store(out_dir / "store-prepared"), [], load_prepared()["label"])}
    syn, obs, _log = synthetic_store(out_dir / "store-synthetic")
    stores["synthetic"] = (syn, obs, SYN_LABEL)
    stages = []
    for name, which, mode, cutoff, must, must_not in STAGES:
        st, observations, label = stores[which]
        proj = R.project(R.research_view(R.load_research(st), cutoff, mode, observations), label)
        (out_dir / f"{name}.json").write_text(json.dumps(proj, indent=1, ensure_ascii=False), encoding="utf-8")
        stages.append({"stage": name, "file": f"{name}.json", "mode": mode, "cutoff": cutoff, "items": len(proj["items"]),
                       "must": must, "must_not": must_not})
    (out_dir / "stages.json").write_text(json.dumps(stages, indent=1), encoding="utf-8")
    return stages


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    for s in build(Path(sys.argv[1])):
        print(f"ok - {s['stage']}: {s['mode']} @ {s['cutoff']} -> {s['items']} items")
