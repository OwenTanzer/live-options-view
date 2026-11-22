"""Bounded pages of the retained DTCC/Tradeweb first-seen tape."""
from datetime import datetime, timezone
import zlib

from . import treu


def read_page(archive, cutoff, project, limit, end=None, seq=None):
    path = archive / "events.jsonl"
    older_bytes = older_seq = 0
    events = []
    if path.is_file():
        with path.open("rb") as stream:
            size = stream.seek(0, 2)
            end = size if end is None else min(size, end)
            start = max(0, end - 2 * 1024 * 1024)
            stream.seek(start)
            block = stream.read(end - start)
        if start:
            boundary = block.find(b"\n")
            if boundary < 0:
                raise ValueError("Archive line exceeds replay window")
            start += boundary + 1
            block = block[boundary + 1:]
        lines = block[:block.rfind(b"\n") + 1].splitlines(keepends=True)
        older_bytes = start + sum(map(len, lines[:-limit]))
        events.extend(project(line, None) for line in lines[-limit:])
    if (archive / treu.DATABASE).is_file():
        with treu.reader(archive) as db:
            if seq is None:
                seq = db.execute("SELECT max(seq) FROM reports").fetchone()[0] or 0
            rows = db.execute("SELECT seq,event FROM reports WHERE seq<=? ORDER BY seq DESC LIMIT ?",
                              (seq, limit + 1)).fetchall()
        if len(rows) > limit:
            older_seq = rows.pop()[0]
        events.extend(project(zlib.decompress(blob), number) for number, blob in rows)
    eligible = []
    instant = datetime.fromtimestamp(cutoff / 1000, timezone.utc)
    for event in events:
        try:
            observed = datetime.fromisoformat(event["first_seen"].replace("Z", "+00:00"))
            if observed.tzinfo and observed <= instant:
                eligible.append((observed, event))
        except (KeyError, AttributeError, TypeError, ValueError):
            continue
    eligible.sort(key=lambda item: item[0])
    return [event for _, event in eligible], older_bytes, older_seq
