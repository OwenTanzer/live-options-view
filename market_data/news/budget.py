"""Conservative R2 request and storage reservation for the single News scheduler host."""
import os
import sqlite3
from datetime import datetime, timezone


def install(client, path):
    """Charge before transmission, including SDK retries and both sides of a failed PUT.

    Monthly request allowance $4.40, retained storage allowance $0.30 (20 GB),
    leaving $0.30 for reader traffic and probes. PUT bytes accumulate as an upper
    bound until an operator reconciles actual bucket sizes with the producer stopped.
    """
    db = sqlite3.connect(path, timeout=5, isolation_level=None)
    os.chmod(path, 0o600)
    db.execute("CREATE TABLE IF NOT EXISTS usage (period TEXT PRIMARY KEY, amount INTEGER NOT NULL)")

    def reserve(request, event_name, **kwargs):
        op = event_name.rsplit(".", 1)[-1]
        # Integer nanodollars. Unrecognized operations take the higher rate.
        cost = {"GetObject": 360, "HeadObject": 360, "DeleteObject": 0}.get(op, 4500)
        size = int(request.headers["Content-Length"]) if op == "PutObject" else 0
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        db.execute("BEGIN IMMEDIATE")
        try:
            for key, delta, limit in [(month, cost, 4_400_000_000), ("bytes", size, 20_000_000_000)]:
                db.execute("INSERT INTO usage VALUES (?, ?) ON CONFLICT(period) DO UPDATE SET amount=amount+excluded.amount", (key, delta))
                if db.execute("SELECT amount FROM usage WHERE period=?", (key,)).fetchone()[0] > limit:
                    raise RuntimeError("News budget exhausted: scheduler must pause")
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise

    client.meta.events.register("before-send.s3", reserve)
