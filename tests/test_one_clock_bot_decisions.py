import hashlib
import io
import json
import unittest
from pathlib import Path
import tempfile
import os
from unittest.mock import patch
from types import SimpleNamespace
import uuid

from crassus.crassus.one_clock_decisions import project, replay_rows
from crassus.crassus.one_clock_archive import publish_chunk


class Missing(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class Store:
    def __init__(self):
        self.objects = {}

    def put_object(self, *, Key, Body, IfNoneMatch=None, IfMatch=None, **kw):
        old = self.objects.get(Key)
        if IfNoneMatch and old is not None or IfMatch and hashlib.sha256(old).hexdigest() != IfMatch:
            raise RuntimeError("precondition")
        self.objects[Key] = Body

    def get_object(self, *, Key, **kw):
        if Key not in self.objects:
            raise Missing()
        data = self.objects[Key]
        return {"Body": io.BytesIO(data), "ContentLength":len(data), "ETag":hashlib.sha256(data).hexdigest(),
                "Metadata":{"start-byte":"0","end-byte":str(len(data)),"sha256":hashlib.sha256(data).hexdigest()}}


def decision(name="crassus_oiskew", strategy="oi_skew_qqq"):
    return {"schema_version":"crassus_audit.v1","decision_id":str(uuid.uuid4()),
            "timestamp_utc":"2026-11-20T15:00:02Z","paper_account_username":name,
            "strategy_id":strategy,"strategy_version":"2","run_id":"PRIVATE_RUN",
            "market_snapshot_timestamp":"2026-11-20T15:00:00Z","code_revision":"a123456",
            "account_state_before":{"balance_cash":500,"username":name},
            "audit_origin":"paper","outcome_class":"no_trade","reason":"PRIVATE_REASON",
            "decision":{"action":"no_trade","metadata":{"band_strikes":8,"session_key":"2026-11-20",
                "anchor_observed_at":"2026-11-20T14:30:00Z","session_start_imbalance":0.1,
                "imbalance_ratio":0.25,"delta_from_session_start":0.15,
                "params":{"near_money_pct":0.05,"imbalance_threshold":0.1,"min_change_from_session_start":0.02,"min_band_strikes":4}}}}


class PublicDecisions(unittest.TestCase):
    def test_isolated_runner_image_shares_exact_wire_contract(self):
        root=Path(__file__).resolve().parents[1]
        self.assertEqual((root/"crassus/crassus/one_clock_publications.py").read_bytes(),
                         (root/"market_data/live_market/publications.py").read_bytes())

    def test_worker_wakeup_resume_and_no_duplicate(self):
        from crassus.crassus.one_clock_archive_worker import ArchiveProjection
        with tempfile.TemporaryDirectory() as tmp:
            private,public=Store(),Store()
            data=(json.dumps(decision())+"\n").encode()
            sha=hashlib.sha256(data).hexdigest(); name="decisions-run.jsonl"; namespace="c"*32
            key=f"crassus-ledger/v1/{namespace}/{name}/{0:020d}-{len(data):020d}-{sha}.jsonl"
            private.objects[key]=data
            private.list_objects_v2=lambda **kw: {"Contents":[{"Key":key}],"IsTruncated":False}
            path=Path(tmp)/"owner.json"
            path.write_text(json.dumps({"namespace":namespace,"files":{name:{"offset":len(data)}}}))
            owner=SimpleNamespace(checkpoint=path,client=private,bucket="private")
            state=Path(tmp)/"public.json"
            worker=ArchiveProjection(owner,public,"public",state)
            self.assertTrue(worker.step())
            before=dict(public.objects)
            self.assertFalse(ArchiveProjection(owner,public,"public",state).step())
            self.assertEqual(public.objects,before)
            state.unlink()
            self.assertTrue(worker.step())
            self.assertEqual(public.objects,before)

    def test_existing_public_credential_fallback_and_override_isolation(self):
        from crassus.crassus.one_clock_archive_worker import from_environment
        owner=SimpleNamespace(bucket="private")
        settings={"ACCOUNT_ID":"a"*32,"BUCKET":"public","ACCESS_KEY_ID":"p","SECRET_ACCESS_KEY":"s"}
        env={"CRASSUS_OI_SKEW_PUBLIC_"+k:v for k,v in settings.items()}
        with patch.dict(os.environ,env,clear=True), patch("crassus.crassus.oi_skew_publisher._r2_client") as client:
            self.assertEqual(from_environment(owner,Path("/tmp")).bucket,"public")
            client.assert_called_once_with(settings)
            os.environ["CRASSUS_ONE_CLOCK_PUBLIC_BUCKET"]="partial"
            self.assertIsNone(from_environment(owner,Path("/tmp")))
            os.environ.pop("CRASSUS_ONE_CLOCK_PUBLIC_BUCKET")
            os.environ["CRASSUS_ONE_CLOCK_PUBLICATION"]="disabled"
            self.assertIsNone(from_environment(owner,Path("/tmp")))
        self.assertIsNone(project(decision("ibit_execution","oi_skew_ibit_adaptive")))

    def test_projection_and_time(self):
        record=decision()
        rows=replay_rows(record,origin="synthetic",available_at="2026-11-20T15:01:00Z")
        self.assertEqual(rows[0]["value"]["inputAgeMs"],2000)
        self.assertEqual(rows[1]["value"]["delta"],0.15)
        self.assertEqual(rows[0]["origin"],"synthetic")
        self.assertEqual(rows[0]["id"],rows[1]["id"])
        self.assertNotIn("PRIVATE",json.dumps(rows))
        self.assertNotIn(record["decision_id"],json.dumps(rows))
        with self.assertRaises(ValueError):
            replay_rows(record,origin="recorded",available_at="2026-11-20T16:00:00Z")
        with self.assertRaises(ValueError):
            replay_rows(record,origin="reconstructed",available_at="2026-11-20T14:00:00Z")
        record["decision"]["metadata"]["anchor_observed_at"]="2026-11-20T15:10:00Z"
        self.assertIsNone(project(record)[1][1]["value"])

    def test_flow_pair_decisions_and_evaluations(self):
        for name in ("crassus_flowskew", "crassus_flow_recover"):
            with self.subTest(bot=name):
                record = decision(name, "flow_skew_qqq")
                record["strategy_decided_at"] = "2026-11-20T15:00:01Z"
                instrument, rows = project(record)
                self.assertEqual(instrument, "QQQ")
                self.assertEqual([row["source"] for row in rows], ["bot-decision", "bot-evaluation"])
                self.assertTrue(all(row["value"]["bot"] == name for row in rows))
                self.assertEqual(rows[1]["eventAt"], "2026-11-20T15:00:01+00:00")
                self.assertNotIn("PRIVATE", json.dumps(rows))
                self.assertIsNone(project(decision(name, "oi_skew_qqq")))

    def test_identity(self):
        record=decision("crassus_skew_ibit","oi_skew_ibit_adaptive")
        record.pop("account_state_before");record["outcome_class"]="transport_error"
        self.assertEqual(project(record)[0],"IBIT")
        self.assertEqual(project(record)[1][0]["value"]["outcome"],"transport_error")
        record["account_state_after"]={"username":"private_user"}
        self.assertIsNone(project(record))
        for name,strategy in [("private_user","oi_skew_qqq"),("crassus_luigi","reddit_sentiment_qqq"),
                              ("crassus_oiskew","momentum_qqq")]:
            self.assertIsNone(project(decision(name,strategy)))

    def test_immutable_verified_batch_retries(self):
        private,public=Store(),Store()
        inputs=[decision() for _ in range(7)]
        inputs.extend([{**decision(),"audit_origin":"simulation"},{**decision(),"audit_origin":None}])
        data=b"".join((json.dumps(v)+"\n").encode() for v in inputs)
        sha=hashlib.sha256(data).hexdigest()
        key=f"crassus-ledger/v1/{'a'*32}/decisions-run.jsonl/{0:020d}-{len(data):020d}-{sha}.jsonl"
        private.objects[key]=data
        receipt=publish_chunk(private,"private",public,"public",key)
        self.assertEqual(len(receipt["commits"]),2)
        self.assertEqual(receipt["unidentifiedOrIneligible"],2)
        before=dict(public.objects)
        self.assertEqual(publish_chunk(private,"private",public,"public",key),receipt)
        self.assertEqual(public.objects,before)
        self.assertNotIn(b"PRIVATE",b"".join(public.objects.values()))
        private.objects[key]=data.replace(b"PRIVATE",b"CHANGED",1)
        with self.assertRaises(ValueError):
            publish_chunk(private,"private",public,"public",key)
