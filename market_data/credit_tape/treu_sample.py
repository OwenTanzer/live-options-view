"""Turn one retained TREU minute file into a new offline credit-tape preview archive."""
import argparse
import csv
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import tempfile


SOURCE = "tradeweb-uk-mtf-treu"
REQUIRED = {"Trading date and time", "Publication date and time", "Venue of publication",
            "Transaction identification code", "Instrument identification code"}


def import_sample(path: Path, destination: Path) -> int:
    match = re.fullmatch(r"UK_TREU_(\d{8})_(\d{4})\.csv", path.name)
    if not match:
        raise ValueError("Expected a UK_TREU minute CSV")
    minute = datetime.strptime("".join(match.groups()), "%Y%m%d%H%M")
    if destination.exists():
        raise FileExistsError(destination)
    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    lines = io.StringIO(payload.decode("utf-8-sig"), newline="")
    metadata = {}
    for fields in csv.reader(lines, strict=True):
        if not fields:
            break
        if len(fields) != 1 or "=" not in fields[0]:
            raise ValueError("Invalid TREU file preamble")
        key, value = fields[0].split("=", 1)
        metadata[key] = value
    if metadata.get("FileName") != path.stem or metadata.get("FileDate") != match[1] or metadata.get("FileTime") != minute.strftime("%H:%M"):
        raise ValueError("TREU preamble disagrees with the filename")
    reader = csv.DictReader(lines, strict=True)
    headers = reader.fieldnames or []
    if not REQUIRED.issubset(headers) or len(set(headers)) != len(headers):
        raise ValueError("Invalid TREU columns")
    first_seen = datetime.now(timezone.utc).isoformat()
    ref = f"{SOURCE}/raw/{minute:%Y-%m-%d}/{path.name}.gz"
    with tempfile.TemporaryDirectory(dir=destination.parent) as staging:
        archive = Path(staging) / "archive"
        raw = archive / ref
        raw.parent.mkdir(parents=True)
        raw.write_bytes(gzip.compress(payload, mtime=0))
        count = 0
        with (archive / "events.jsonl").open("w", encoding="utf-8", newline="\n") as output:
            for index, row in enumerate(reader):
                if None in row or None in row.values() or row["Venue of publication"] != "TREU":
                    raise ValueError(f"Malformed row or unexpected publishing venue at row {index}")
                event = {
                    "kind": "publication", "source": SOURCE, "publisher": "Tradeweb",
                    "jurisdiction": "UK", "channel": "TREU", "route": "delayed-minute-csv",
                    "qualification": "Unclassified sample", "backfill": True,
                    "first_seen": first_seen, "execution_ts": row["Trading date and time"],
                    "dissemination_ts": row["Publication date and time"],
                    "id": row["Transaction identification code"], "record": row,
                    "row_index": index, "raw_ref": ref, "raw_sha256": digest,
                }
                output.write(json.dumps(event, separators=(",", ":")) + "\n")
                count += 1
        archive.rename(destination)
    return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("new_archive", type=Path)
    args = parser.parse_args()
    print(f"Imported {import_sample(args.csv, args.new_archive)} unclassified sample rows")
