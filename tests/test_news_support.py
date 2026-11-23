"""Shared helpers for the news archive tests (no tests of its own)."""
import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from market_data.news.__main__ import fixture_fetcher  # noqa: E402
from market_data.news.archive import LocalObjectStore  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "news"


def at(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


class Clock:
    """Settable clock; the archive code reads it, tests move it."""

    def __init__(self, text="2026-11-09T10:00:00Z"):
        self.now = at(text)

    def set(self, text):
        self.now = at(text)
        return self

    def __call__(self):
        return self.now


def fixed(text):
    value = at(text)
    return lambda: value


def feeds(name="feeds"):
    return fixture_fetcher(FIXTURES / name)


def no_sleep(_seconds):
    return None


class FailingStore(LocalObjectStore):
    """Local store that raises once a put matches `fail_on` (simulated crash)."""

    def __init__(self, root, fail_on: str):
        super().__init__(root)
        self.fail_on = fail_on

    def put(self, key, data, content_type="application/octet-stream", cache_control=None):
        if self.fail_on and self.fail_on in key:
            raise RuntimeError(f"simulated crash writing {key}")
        super().put(key, data, content_type, cache_control=cache_control)

    def put_if_absent(self, key, data, content_type="application/octet-stream", cache_control=None):
        if self.fail_on and self.fail_on in key:
            raise RuntimeError(f"simulated crash writing {key}")
        return super().put_if_absent(key, data, content_type=content_type, cache_control=cache_control)
