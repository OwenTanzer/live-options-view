"""Publish closed receipt minutes off the capture thread while live transport runs."""
import json
import logging

from .intraminute_archive import PREFIX


def publish_closed(tape, client, bucket, stop, lost):
    cursor, epoch, minute = 0, "", None
    rows = []
    complete = False
    gap = False
    while not stop.is_set() and not lost.is_set():
        batch = tape.read(cursor, epoch)
        if batch["reset"]:
            minute, rows, complete, gap = None, [], False, False
        for row in batch["rows"]:
            current = row["at"] // 60000 * 60000
            if current != minute:
                if minute is not None and current < minute:
                    complete = False
                    continue
                if complete and rows and not lost.is_set() and not stop.is_set():
                    body = dict(version=1, minute=minute, rows=rows, source="session-provisional")
                    try:
                        client.put_object(Bucket=bucket, Key=f"{PREFIX}/{minute}.json",
                                          ContentType="application/json", CacheControl="no-store",
                                          IfNoneMatch="*", Body=json.dumps(body,separators=(",", ":")).encode())
                    except Exception as exc:
                        logging.warning("QQQ provisional minute %s: %s", minute,type(exc).__name__)
                complete = (minute is not None or
                            (not batch["truncated"] and tape.started <= current)) and not gap
                minute, rows = current, []
            if row["kind"] == "gap":
                gap = row["reason"] == "stream_disconnect"
            # If a burst exceeds the bounded buffer, defer repair to raw replay.
            if len(rows) >= tape.rows.maxlen:
                complete = False
            elif complete:
                rows.append(row)
        cursor, epoch = batch["cursor"], batch["epoch"]
        if not batch["more"]:
            stop.wait(0.5)
