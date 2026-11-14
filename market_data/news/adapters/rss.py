"""RSS 2.0 / RSS 1.0 (RDF) / Atom adapter with bounded, injected fetching.

Parsing uses the standard library so raw source time strings survive exactly
as published (feedparser silently assumes UTC for zone-less dates). The fetch
function is injected: tests and the default CLI mode use fixtures; the
network fetcher is only used when explicitly configured.
"""

from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from .. import config
from ..normalize import iso


class FetchError(Exception):
    """Transport failure (timeout, connection, TLS). Retried."""


@dataclass
class FetchResponse:
    status: int
    body: bytes


@dataclass
class SourceResult:
    source_id: str
    status: str  # ok | http_error | fetch_error | parse_error | budget_exceeded | disabled
    attempts: int = 0
    http_status: int | None = None
    error: str = ""
    observed_at: str | None = None
    entries: list[dict] = field(default_factory=list)
    bytes: int = 0


# ── parsing ─────────────────────────────────────────────────────────────────

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(el, name: str):
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _text(el, name: str) -> str | None:
    c = _child(el, name)
    if c is None:
        return None
    return (c.text or "").strip() or None


def _rss_item(el) -> dict:
    source = _child(el, "source")
    return {
        "guid": _text(el, "guid"),
        "link": _text(el, "link"),
        "title": _text(el, "title") or "",
        "summary": _text(el, "description") or _text(el, "encoded") or "",
        # RSS 2.0 pubDate, else Dublin Core date (RSS 1.0 / many 2.0 feeds).
        "published_raw": _text(el, "pubDate") or _text(el, "date"),
        "updated_raw": _text(el, "updated") or _text(el, "modified"),
        "outlet": (source.text or "").strip() or None if source is not None else None,
        "categories": [(c.text or "").strip() for c in el if _local(c.tag) in ("category", "subject") and (c.text or "").strip()],
    }


def _atom_link(el) -> str | None:
    fallback = None
    for c in el:
        if _local(c.tag) != "link":
            continue
        rel = c.get("rel", "alternate")
        href = c.get("href")
        if rel == "alternate" and href:
            return href
        fallback = fallback or href
    return fallback


def _atom_entry(el) -> dict:
    source = _child(el, "source")
    return {
        "guid": _text(el, "id"),
        "link": _atom_link(el),
        "title": _text(el, "title") or "",
        "summary": _text(el, "summary") or _text(el, "content") or "",
        "published_raw": _text(el, "published") or _text(el, "issued"),
        "updated_raw": _text(el, "updated") or _text(el, "modified"),
        "outlet": _text(source, "title") if source is not None else None,
        "categories": [c.get("term", "") for c in el if _local(c.tag) == "category" and c.get("term")],
    }


def parse_feed(content: bytes) -> list[dict]:
    """Parse feed bytes into raw entries. Raises ValueError on malformed XML."""
    if b"<!ENTITY" in content[:4096]:
        raise ValueError("feed declares XML entities; refusing to expand")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError(f"malformed feed XML: {exc}") from None
    kind = _local(root.tag)
    if kind == "feed":
        return [_atom_entry(e) for e in root if _local(e.tag) == "entry"]
    if kind in ("rss", "RDF"):
        items = [e for e in root.iter() if _local(e.tag) == "item"]
        return [_rss_item(e) for e in items]
    raise ValueError(f"unrecognized feed root <{kind}>")


# ── fetching ────────────────────────────────────────────────────────────────

USER_AGENT = "OptionsView-NewsArchive/1.0 (+https://github.com/OwenTanzer/live-options-view)"
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


def http_fetch(url: str, timeout: float) -> FetchResponse:
    """Network fetcher. Only used when the CLI is explicitly run with --network."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(config.MAX_RESPONSE_BYTES + 1)
            if len(body) > config.MAX_RESPONSE_BYTES:
                raise FetchError(f"response exceeds {config.MAX_RESPONSE_BYTES} bytes")
            return FetchResponse(resp.status, body)
    except urllib.error.HTTPError as exc:
        return FetchResponse(exc.code, b"")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchError(type(exc).__name__ + ": " + str(getattr(exc, "reason", exc))[:200]) from None


def fetch_source(source, fetch, clock, sleep=time.sleep,
                 timeout: float = config.REQUEST_TIMEOUT_S,
                 retries: int = config.MAX_RETRIES,
                 backoff: tuple[float, ...] = config.RETRY_BACKOFF_S) -> SourceResult:
    result = SourceResult(source_id=source.id, status="fetch_error")
    for attempt in range(retries + 1):
        result.attempts = attempt + 1
        try:
            resp = fetch(source.url, timeout)
        except FetchError as exc:
            result.status, result.error = "fetch_error", str(exc)[:300]
        else:
            result.http_status = resp.status
            if resp.status == 200:
                result.observed_at = iso(clock())
                result.bytes = len(resp.body)
                try:
                    result.entries = parse_feed(resp.body)
                except ValueError as exc:
                    result.status, result.error = "parse_error", str(exc)[:300]
                    return result
                result.status, result.error = "ok", ""
                return result
            result.status, result.error = "http_error", f"HTTP {resp.status}"
            if resp.status not in RETRYABLE_STATUS:
                return result
        if attempt < retries:
            sleep(backoff[min(attempt, len(backoff) - 1)])
    return result


class FetchPool:
    """Bounded worker pool shared by every cycle in a process.

    A thread cannot be killed, so a fetch still running when its cycle's budget
    expires keeps its worker until it returns. Sharing one pool of
    max_workers threads across cycles bounds that carry-over: a later cycle
    can never add workers beyond the limit, and its sources queue behind (and
    are recorded as budget_exceeded if they never start) instead of spawning
    more threads.
    """

    def __init__(self, max_workers: int = config.MAX_CONCURRENT_SOURCES):
        self.max_workers = max_workers
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="news-fetch")
        self._lock = threading.Lock()
        self._running = 0

    def submit(self, fn, *args):
        def run():
            with self._lock:
                self._running += 1
            try:
                return fn(*args)
            finally:
                with self._lock:
                    self._running -= 1
        return self._executor.submit(run)

    def busy(self) -> int:
        """Workers currently running a fetch (including ones from earlier cycles)."""
        with self._lock:
            return self._running

    def threads(self) -> int:
        return len(self._executor._threads)


_POOLS: dict[int, FetchPool] = {}
_POOLS_LOCK = threading.Lock()


def default_pool(max_workers: int = config.MAX_CONCURRENT_SOURCES) -> FetchPool:
    with _POOLS_LOCK:
        if max_workers not in _POOLS:
            _POOLS[max_workers] = FetchPool(max_workers)
        return _POOLS[max_workers]


def fetch_all(sources, fetch, clock, sleep=time.sleep,
              max_concurrent: int = config.MAX_CONCURRENT_SOURCES,
              budget_s: float = config.CYCLE_BUDGET_S, monotonic=time.monotonic,
              pool: FetchPool | None = None) -> dict[str, SourceResult]:
    """Fetch every source with bounded concurrency and an overall cycle budget.

    Work runs on a pool shared across cycles (see FetchPool), so successive
    budget-expired cycles never hold more than max_concurrent workers between
    them. A source still running when the budget expires, or still queued
    behind earlier work, is recorded as budget_exceeded (a late result is
    discarded), never as an empty success.
    """
    results: dict[str, SourceResult] = {}
    if not sources:
        return results
    pool = pool or default_pool(max_concurrent)
    started = monotonic()
    futures = {pool.submit(fetch_source, s, fetch, clock, sleep): s for s in sources}
    done, pending = wait(futures, timeout=max(0.0, budget_s - (monotonic() - started)))
    for fut in done:
        src = futures[fut]
        try:
            results[src.id] = fut.result()
        except Exception as exc:  # adapter bug: record, don't lose the cycle
            results[src.id] = SourceResult(src.id, "fetch_error", error=f"{type(exc).__name__}: {exc}"[:300])
    for fut in pending:
        src = futures[fut]
        never_started = fut.cancel()
        detail = ("never started: all fetch workers were still busy" if never_started
                  else "still running; its worker is released when the fetch returns")
        results[src.id] = SourceResult(src.id, "budget_exceeded",
                                       error=f"cycle budget {budget_s:.0f}s exceeded ({detail})")
    return results
