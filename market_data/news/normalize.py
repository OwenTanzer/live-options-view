"""Turn parsed feed entries into normalized, revision-addressed observations.

Rules that differ from the prototypes on purpose:
  * canonical URLs normalize only scheme/host and known tracking parameters;
    path and query values keep their case (the prototypes lowercased them);
  * a missing or zone-less source time stays null with a reason; collection
    time is never substituted for publication time;
  * publication and update times are kept separately;
  * a changed title/excerpt/time/URL/category at a known identity is a new
    revision, not a "seen" item to skip. Categories are classifier input, so
    they are part of the revision payload;
  * a link that cannot be parsed (for example a non-numeric port) never
    aborts the cycle: it has no canonical URL and is marked url_status
    "invalid", and identity falls back to the GUID or title.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_tz, mktime_tz
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import ADAPTER_VERSION, MAX_EXCERPT_CHARS, SCHEMA_VERSION

TRACKING_PARAMS = frozenset({"fbclid", "gclid", "dclid", "msclkid", "ocid", "cmpid", "mc_cid", "mc_eid", "_hsenc", "_hsmi"})


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def canonical_json(payload) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def iso(dt: datetime) -> str:
    dt = dt.astimezone(timezone.utc)
    if dt.microsecond:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def canonical_url(raw: str | None) -> str:
    """Normalized absolute http(s) URL, or "" when there is none.

    Lowercases scheme and host, drops a leading "www.", default ports, the
    fragment and known tracking parameters. Path and remaining query values are
    case-sensitive and kept as given. Bing click-through links are unwrapped to
    their target, as in the prototypes; no redirect is ever fetched.
    """
    raw = (raw or "").strip()
    try:
        parts = urlsplit(raw)
        parts.port  # raises ValueError for a non-numeric or out-of-range port
    except ValueError:
        return ""
    if parts.netloc.lower().endswith("bing.com") and parts.path.lower().endswith("/news/apiclick.aspx"):
        target = dict(parse_qsl(parts.query)).get("url", "")
        if target:
            return canonical_url(target)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        return ""
    host = parts.hostname.lower().removeprefix("www.")
    port = parts.port if parts.port and parts.port not in (80, 443) else None
    netloc = f"{host}:{port}" if port else host
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS]
    return urlunsplit((scheme, netloc, parts.path or "/", urlencode(query), ""))


def url_status(raw: str | None, canonical: str) -> str:
    """ok | missing | invalid (present but not a usable absolute http(s) URL)."""
    if canonical:
        return "ok"
    return "invalid" if (raw or "").strip() else "missing"


def identity_url(canonical: str) -> str:
    """Scheme-insensitive form used for identity (http and https are one article)."""
    return canonical.split("://", 1)[-1] if canonical else ""


def clean_text(raw: str | None) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def normalized_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", clean_text(title).lower()).strip()


_RFC2822_ZONE = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?\s*(?P<zone>[+-]\d{4}|[A-Za-z]{1,5})?\s*$")
_RFC822_ZONES = frozenset({"UT", "UTC", "GMT", "Z", "EST", "EDT", "CST", "CDT", "MST", "MDT", "PST", "PDT"})


def parse_source_time(raw: str | None) -> tuple[str | None, str]:
    """(UTC ISO time or None, quality). Never guesses a zone.

    Quality: explicit | missing | no_timezone | unknown_offset | unknown_zone |
    date_only | unparseable.
    """
    text = (raw or "").strip()
    if not text:
        return None, "missing"
    parsed = parsedate_tz(text)
    if parsed is not None:
        # email.utils reports offset 0 for a missing, "-0000" or unrecognized
        # zone alike, so the zone token is checked explicitly.
        m = _RFC2822_ZONE.search(text)
        zone = m.group("zone") if m else None
        if not zone:
            return None, "no_timezone"
        if zone == "-0000":
            return None, "unknown_offset"
        if zone[0] not in "+-" and zone.upper() not in _RFC822_ZONES:
            return None, "unknown_zone"
        return iso(datetime.fromtimestamp(mktime_tz(parsed), timezone.utc)), "explicit"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return None, "date_only"
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None, "unparseable"
    if dt.tzinfo is None:
        return None, "no_timezone"
    return iso(dt), "explicit"


def item_identity(source_id: str, canonical: str, guid: str | None, title: str) -> tuple[str, str]:
    """(item_id, identity_quality). Distinct sources never share an identity."""
    if canonical:
        return sha256_hex(f"url\x00{source_id}\x00{identity_url(canonical)}"), "url"
    if guid and guid.strip():
        return sha256_hex(f"guid\x00{source_id}\x00{guid.strip()}"), "guid"
    return sha256_hex(f"title\x00{source_id}\x00{normalized_title(title)}"), "weak"


def normalized_categories(raw) -> list[str]:
    """Order- and duplicate-insensitive category list (classifier input)."""
    return sorted({clean_text(c) for c in (raw or []) if clean_text(c)})


def revision_payload(title: str, excerpt: str, published_raw, updated_raw, canonical: str,
                     categories: list[str]) -> dict:
    # Everything the archive keeps from the source that a classifier reads is
    # in the payload, so a change to any of it is a new, dated revision.
    return {"title": title, "excerpt": excerpt, "published_raw": published_raw,
            "updated_raw": updated_raw, "canonical_url": canonical, "categories": categories}


def revision_id(item_id: str, payload_digest: str, previous_revision_id: str | None) -> str:
    # Chained so that A -> B -> A yields three distinct revision events.
    return sha256_hex(f"{item_id}\x00{payload_digest}\x00{previous_revision_id or ''}")


def normalize_entry(entry: dict, source, run_id: str, observed_at: str, config_digest: str,
                    state: dict) -> tuple[dict | None, str]:
    """Return (observation or None, outcome) and update `state` in place.

    `state` maps item_id -> {latest_revision_id, payload_digest, first_seen_at}
    reconstructed from committed history. Outcome is "new_item",
    "new_revision" or "unchanged"; unchanged entries produce no record (they
    are counted as coverage evidence in the run manifest).
    """
    title = clean_text(entry.get("title"))
    excerpt_full = clean_text(entry.get("summary"))
    excerpt = excerpt_full[:MAX_EXCERPT_CHARS]
    original_url = (entry.get("link") or "").strip()
    canonical = canonical_url(original_url)
    item_id, quality = item_identity(source.id, canonical, entry.get("guid"), title)
    published_raw = entry.get("published_raw")
    updated_raw = entry.get("updated_raw")
    categories = normalized_categories(entry.get("categories"))
    payload = revision_payload(title, excerpt, published_raw, updated_raw, canonical, categories)
    payload_digest = sha256_hex(canonical_json(payload))

    known = state.get(item_id)
    if known and known["payload_digest"] == payload_digest:
        return None, "unchanged"
    previous = known["latest_revision_id"] if known else None
    rev = revision_id(item_id, payload_digest, previous)
    first_seen = known["first_seen_at"] if known else observed_at
    published_at, published_q = parse_source_time(published_raw)
    updated_at, updated_q = parse_source_time(updated_raw)
    record = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "observation",
        "run_id": run_id,
        "source_id": source.id,
        "source_name": source.name,
        "source_streams": list(source.streams),
        "adapter_version": ADAPTER_VERSION,
        "config_digest": config_digest,
        "item_id": item_id,
        "identity_quality": quality,
        "original_url": original_url,
        "canonical_url": canonical,
        "url_status": url_status(original_url, canonical),
        "source_guid": entry.get("guid"),
        "outlet": entry.get("outlet"),
        "categories": categories,
        "revision_id": rev,
        "previous_revision_id": previous,
        "payload_digest": payload_digest,
        "title": title,
        "excerpt": excerpt,
        "excerpt_truncated": len(excerpt_full) > len(excerpt),
        "content_kind": "feed_entry",
        "published_raw": published_raw,
        "published_at": published_at,
        "updated_raw": updated_raw,
        "updated_at": updated_at,
        "time_quality": {"published": published_q, "updated": updated_q},
        "observed_at": observed_at,
        "first_seen_at": first_seen,
    }
    state[item_id] = {"latest_revision_id": rev, "payload_digest": payload_digest, "first_seen_at": first_seen}
    return record, ("new_revision" if known else "new_item")
