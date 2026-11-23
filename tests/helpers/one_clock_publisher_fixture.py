"""Dependency-free Python publisher wire fixture for the JS consumer suite."""
import base64
import importlib.util
import io
import json
from pathlib import Path
import uuid

spec = importlib.util.spec_from_file_location("publication_fixture", Path(__file__).resolve().parents[2] / "market_data/live_market/publications.py")
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class Missing(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class Store:
    def __init__(self):
        self.data = {}

    def get_object(self, *, Bucket, Key):
        if Key not in self.data:
            raise Missing()
        return {"Body": io.BytesIO(self.data[Key]), "ETag": publisher.digest(self.data[Key])}

    def put_object(self, *, Bucket, Key, Body, ContentType, IfNoneMatch=None, IfMatch=None):
        if IfNoneMatch and Key in self.data or IfMatch and publisher.digest(self.data.get(Key, b"")) != IfMatch:
            raise RuntimeError("precondition")
        self.data[Key] = Body


store = Store()
log = publisher.PublicationLog(store, "fixture", source="collector-chain", instrument="QQQ", owner="market_data.live_market", outputs={"price": {"spot": "number"}})
for i, (identity, revision, value, minute) in enumerate((("snapshot0", 1, 699, 0), ("snapshot1", 1, 701, 1), ("snapshot1", 2, 698, 1), ("snapshot1", 3, None, 1), ("old-session", 1, 705, -1))):
    session = "2026-11-19" if minute < 0 else "2026-11-20"
    event = f"{session}T15:0{max(0, minute)}:00Z"
    log.publish(publication_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"interop/{i}")),
                records=[{"source": "price", "id": identity, "revision": revision, "session": session, "eventAt": event, "value": None if value is None else {"spot": value}}],
                evidence=[(b"public fixture evidence", "text/csv", "fixture.v1")], created_at=event)
print(json.dumps([{ "key": key, "wire": base64.b64encode(raw).decode()} for key, raw in sorted(store.data.items()) if "/entries/" in key]))
