"""Publish one imported crypto snapshot to the private history bucket."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3

import boto3
from botocore.exceptions import ClientError


def publish(database, bucket):
    client = boto3.client('s3', endpoint_url=os.environ['R2_ENDPOINT_URL'],
                          aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],
                          aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'])
    uri = database.resolve().as_uri() + '?mode=ro&immutable=1'
    with sqlite3.connect(uri, uri=True) as db:
        metadata = json.loads(db.execute('SELECT json FROM receipt').fetchone()[0])

    def write_series(series):
        symbol, interval, provider = (series[k] for k in ('symbol', 'interval', 'provider'))
        with sqlite3.connect(uri, uri=True) as db:
            records = db.execute('SELECT ts,event,value FROM prices WHERE symbol=? AND interval=? '
                                 'AND provider=? ORDER BY ts', (symbol, interval, provider))
            rows = [dict(t=t, event=event, **json.loads(value)) for t, event, value in records]
        if len(rows) != series['count']:
            raise ValueError(f'Count mismatch for {symbol} {interval} {provider}')
        snapshot = metadata['archive_sha256']
        payload = dict(series=series, archive_sha256=snapshot, available_at=metadata['available_at'], rows=rows)
        body = gzip.compress(json.dumps(payload, separators=(',', ':'), allow_nan=False).encode(), mtime=0)
        key = f'crypto/{snapshot}/{symbol}/{interval}/{provider}.json.gz'
        digest = hashlib.sha256(body).hexdigest()
        try:
            client.put_object(Bucket=bucket, Key=key, Body=body, IfNoneMatch='*',
                              Metadata={'sha256': digest}, ContentType='application/json', ContentEncoding='gzip')
        except ClientError as error:
            if error.response['Error']['Code'] not in ('412', 'PreconditionFailed'):
                raise
        stored = client.get_object(Bucket=bucket, Key=key)['Body'].read()
        if hashlib.sha256(stored).hexdigest() != digest:
            raise ValueError(f'Readback mismatch at {key}')
        return len(rows)

    with ThreadPoolExecutor(max_workers=3) as pool:
        counts = list(pool.map(write_series, metadata['coverage']))
    print(f"Verified {len(counts)} series and {sum(counts)} observations in {bucket}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('database', type=Path)
    args = parser.parse_args()
    publish(args.database, 'optionsview-private-history')
