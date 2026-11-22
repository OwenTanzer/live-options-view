"""Read a verified Crassus archive chunk, publish its allowlisted projection.

An on-demand consumer of the existing private backup. Raw records are never
written to the public transport. Command-line publication requires reading
the real immutable input through its private object-store settings.
"""
import argparse
import hashlib
import json
import os
import re
import uuid

from .one_clock_decisions import project
from . import one_clock_publications as wire

FIELDS = {
    "bot-decision": {"bot": "string", "outcome": "string", "action": "string", "inputAgeMs": "number"},
    "bot-anchored-skew": {"anchor": "number", "imbalance": "number", "delta": "number",
                          "strikes": "number", "anchorAt": "string", "run": "string",
                          "threshold": "number", "minChange": "number"},
}
KEY = re.compile(r"crassus-ledger/v1/[0-9a-f]{32}/decisions-[A-Za-z0-9_-]+\.jsonl/"
                 r"([0-9]{20})-([0-9]{20})-([0-9a-f]{64})\.jsonl")
MAX_BYTES = 9 * 1024 * 1024


def publish_chunk(private, private_bucket, public, public_bucket, key):
    match = KEY.fullmatch(key)
    if private_bucket == public_bucket or not match:
        raise ValueError("Distinct private/public buckets and a valid immutable ledger key are required")
    start, end, expected = match.groups()
    size = int(end) - int(start)
    if not 0 < size <= MAX_BYTES:
        raise ValueError("archive range exceeds bound")
    obj = private.get_object(Bucket=private_bucket, Key=key)
    body = obj["Body"]
    try:
        data = body.read(size + 1)
    finally:
        body.close()
    if (len(data) != size or obj.get("ContentLength") != size or
            obj.get("Metadata") != {"sha256": expected, "start-byte": str(int(start)), "end-byte": str(int(end))} or
            hashlib.sha256(data).hexdigest() != expected or not data.endswith(b"\n")):
        raise ValueError("archive evidence mismatch")
    groups, ignored = {}, 0
    for line in data.splitlines():
        record = json.loads(line)
        if (record.get("audit_origin") != "paper" or
                str(record.get("reason") or "").startswith("[dry-run]")):
            ignored += 1
            continue
        projected = project(record)
        if projected is None:
            ignored += 1
            continue
        instrument, rows = projected
        batches = groups.setdefault(instrument, [[]])
        if len(batches[-1]) + len(rows) > 10:
            batches.append([])
        batches[-1].extend(rows)
    commits = []
    for instrument, batches in groups.items():
        for index, rows in enumerate(batches):
            publisher = wire.PublicationLog(wire.BudgetedS3(public, seconds=15, requests=96), public_bucket,
                source="bot-audit", instrument=instrument, owner="crassus", outputs=FIELDS)
            # The only public evidence bytes are the same allowlisted projection.
            evidence = wire.wire(rows)
            public_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"one-clock/bot-audit/v1/{key}/{instrument}/{index}"))
            created = max((wire.PublicationLog._time(row["eventAt"]) for row in rows)).isoformat()
            result = publisher.publish(publication_id=public_id, records=rows, created_at=created,
                evidence=[(evidence, "application/json", "crassus.public_decisions.v1")])
            commits.append({"stream": result["stream"], "sequence": result["sequence"]})
    return {"commits": commits, "unidentifiedOrIneligible": ignored}


def main():
    import boto3
    from botocore.config import Config
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive_key")
    args = parser.parse_args()
    if os.environ.get("CRASSUS_ARCHIVE_PRIVATE") != "true":
        raise SystemExit("A private archive source is required")
    def connection(prefix):
        env = {key: os.environ[prefix + key] for key in ("ACCOUNT_ID","ACCESS_KEY_ID","SECRET_ACCESS_KEY","BUCKET")}
        client = boto3.client("s3", endpoint_url=f"https://{env['ACCOUNT_ID']}.r2.cloudflarestorage.com",
            aws_access_key_id=env["ACCESS_KEY_ID"], aws_secret_access_key=env["SECRET_ACCESS_KEY"],
            region_name="auto", config=Config(connect_timeout=3,read_timeout=10,retries={"total_max_attempts":2}))
        return client, env["BUCKET"]
    print(json.dumps(publish_chunk(*connection("CRASSUS_ARCHIVE_"), *connection("CRASSUS_ONE_CLOCK_PUBLIC_"),
                                   args.archive_key)))


if __name__ == "__main__":
    main()
