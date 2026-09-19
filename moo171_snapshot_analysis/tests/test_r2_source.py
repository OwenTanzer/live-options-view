import hashlib
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from r2_source import SnapshotSource


def _seed_cache(cache_dir: Path, yyyymmdd: str, entries: list[tuple[str, bytes]]) -> None:
    """Pre-populate cache-only mode (s3=None) with a listing marker and the
    given (key, body) pairs already on disk -- mirrors what a prior
    real fetch would have left behind, without needing a fake S3 client."""
    prefix = f"intraday/{yyyymmdd}/"
    listing = [{"key": key, "size": len(body), "etag": hashlib.md5(body).hexdigest()} for key, body in entries]
    marker = cache_dir / prefix / "_listing.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(listing))
    for key, body in entries:
        path = cache_dir / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)


def test_get_bytes_records_sha256_of_the_actual_bytes_read():
    with tempfile.TemporaryDirectory() as tmp:
        cache_dir = Path(tmp)
        body = b"date,snapshot_key\n20260910,snap1\n"
        key = "intraday/20260910/snapshot_060002893036.csv"
        _seed_cache(cache_dir, "20260910", [(key, body)])
        source = SnapshotSource(cache_dir=cache_dir, s3=None)
        rows = source.snapshot_csv_rows(key)
        assert len(rows) == 1
        assert source._read_sha256[key] == hashlib.sha256(body).hexdigest()


def test_verified_manifest_merges_listing_metadata_with_read_sha256():
    with tempfile.TemporaryDirectory() as tmp:
        cache_dir = Path(tmp)
        body = b"a,b\n1,2\n"
        key = "intraday/20260910/snapshot_060002893036.csv"
        _seed_cache(cache_dir, "20260910", [(key, body)])
        source = SnapshotSource(cache_dir=cache_dir, s3=None)
        source.snapshot_csv_rows(key)  # actually reads/parses -- populates _read_sha256

        manifest = source.verified_manifest("20260910")
        assert len(manifest) == 1
        entry = manifest[0]
        assert entry["key"] == key
        assert entry["sha256"] == hashlib.sha256(body).hexdigest()


def test_verified_manifest_reports_none_sha256_for_a_key_never_actually_read():
    with tempfile.TemporaryDirectory() as tmp:
        cache_dir = Path(tmp)
        key = "intraday/20260910/snapshot_060002893036.csv"
        # Listed but never fetched (no cached body on disk) -- e.g. a
        # listing-only pass that hasn't parsed this object yet.
        prefix = "intraday/20260910/"
        marker = cache_dir / prefix / "_listing.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps([{"key": key, "size": 10, "etag": "deadbeef"}]))
        source = SnapshotSource(cache_dir=cache_dir, s3=None)

        manifest = source.verified_manifest("20260910")
        assert manifest[0]["sha256"] is None
