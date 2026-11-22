"""Authenticated Options View credit-tape table."""
from datetime import datetime
from decimal import Decimal, InvalidOperation
from html import escape
from hashlib import sha256
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")


def h(value):
    return escape(str(value if value is not None else ""))


def seen(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(PACIFIC)
    except (ValueError, AttributeError, OverflowError):
        return None


def amount(raw):
    if raw is None or raw == "":
        return "—"
    value = str(raw)
    try:
        n = Decimal(value.rstrip("+").replace(",", ""))
        if not n.is_finite() or abs(n.adjusted()) > 40:
            return value
    except InvalidOperation:
        return value
    for unit, divisor in [("B", 10**9), ("M", 10**6), ("K", 1000), ("", 1)]:
        if abs(n) >= divisor or not unit:
            return format((n / divisor).normalize(), "f") + unit + ("+" if value.endswith("+") else "")


def render_table(events, cursor, has_sample=False, sample_view=False, has_treu=False, treu_view=False, replay=None):
    rows = []
    anchors = {}
    for ev in reversed(events):
        if ev.get("kind") != "publication":
            continue
        s = ev.get("summary") or {}
        first = seen(ev.get("first_seen"))
        stamp = first.strftime("%H:%M:%S") if first else "—"
        anchor = ""
        if first:
            bucket = first.replace(minute=first.minute // 10 * 10, second=0, microsecond=0)
            key = str(int(bucket.timestamp()))
            if key not in anchors:
                anchors[key] = bucket.strftime("%b %d %H:%M")
                anchor = f' id="t{key}"'
        product = s.get("uniqueProductIdentifierShortName")
        name = s.get("underlyingAssetName") or s.get("underlierIDLeg1") or product or s.get("productName") or "—"
        detail = " · ".join(str(v) for v in (product if product != name else "", s.get("expirationDate")) if v)
        raw_link = ""
        if replay is None and ev.get("source") == "tradeweb-uk-mtf-treu" and "#" in (ev.get("raw_ref") or ""):
            filename, _, digest = ev["raw_ref"].partition("#")
            raw_link = (f'<a href="?view=treu&amp;file={h(filename)}&amp;sha={h(digest)}">'
                        f'Original CSV · row {int(ev["row_index"])+1}</a>')
        action = ev.get("action") or "—"
        channel = (ev.get("jurisdiction") or ev.get("source") or "—").removeprefix("US-")
        publisher = ev.get("publisher") or ev.get("source") or "Unknown publisher"
        if publisher == "DTCC Data Repository (U.S.) LLC":
            publisher = "DTCC"
        venue = ev.get("channel") if channel == "UK" else ""
        origin = " · ".join(str(v) for v in (publisher, channel, venue) if v)
        history = " · ".join(k for k in ("backfill", "replay") if ev.get(k))
        price = " ".join(str(v) for v in (s.get("price"), s.get("priceNotation"), s.get("priceCurrency"))
                         if v not in (None, "")) if s.get("price") not in (None, "") else ""
        fields = [
            ("Report ID", ev.get("id")), ("Original ID", ev.get("original_id")),
            ("Execution", ev.get("execution_ts")), ("Publication", ev.get("dissemination_ts")),
            ("Publisher file updated", ev.get("publisher_file_modified")), ("First seen", ev.get("first_seen")),
            ("Action", ev.get("action")), ("Price", price), ("Fixed rate", s.get("fixedRateLeg1")),
            ("Notional", " ".join(str(v) for v in (s.get("notionalAmountLeg1"), s.get("notionalCurrencyLeg1")) if v is not None)),
            ("Route", ev.get("route")), ("Classification", ev.get("qualification")),
            ("Execution venue", s.get("executionVenue")), ("Identifier type", s.get("instrumentIdType")),
            ("Report flags", s.get("reportFlags")), ("Upfront payment", s.get("upfront")), ("Spread", s.get("spread")),
            ("Platform", s.get("platformIdentifier")), ("Cleared", s.get("cleared")),
            ("Block trade", s.get("blocktradeElectionIndicator")), ("Archive", ev.get("raw_ref")),
        ]
        details = "".join(f"<dt>{h(label)}</dt><dd>{h(value)}</dd>" for label, value in fields if value not in (None, ""))
        identity = repr((ev.get("source"), ev.get("raw_ref"), ev.get("row_index"), ev.get("id"), ev.get("first_seen")))
        row_key = sha256(identity.encode()).hexdigest()
        index = " ".join(str(v) for v in [*s.values(), *(v for _, v in fields)] if v)
        rows.append(f'<tr{anchor} data-publisher="{h(publisher)}" data-search="{h(index)}" data-channel="{h(channel)}" data-action="{h(action)}">'
                    f'<td>{stamp}</td><td><details data-key="{row_key}"><summary>{h(name)}<small>{h(detail)}</small></summary>'
                    f'<dl>{details}</dl>{raw_link}</details></td>'
                    f'<td>{h(publisher)}<small>{h(" · ".join(str(v) for v in (channel, venue) if v))}</small>'
                    f'<small>{h(history or ev.get("action"))}</small></td>'
                    f'<td><b>{h(amount(s.get("notionalAmountLeg1")))}</b><small>{h(s.get("notionalCurrencyLeg1"))}</small></td></tr>')
    links = " ".join(f'<a href="#t{k}">{v}</a>' for k, v in anchors.items())
    switch_view = ""
    if has_sample:
        switch_view = ('<a href="/credit-tape/">Return to live tape</a>' if sample_view else
                       '<a href="/credit-tape/?view=treu-sample">Tradeweb UK sample</a>')
    title = "Tradeweb UK sample" if sample_view else "Credit tape"
    mode = "replay" if replay else "live"
    if replay:
        cutoff, older_bytes, older_seq = replay
        stamp = datetime.fromtimestamp(cutoff / 1000, ZoneInfo("UTC")).isoformat(timespec="milliseconds")
        title = "Credit tape replay"
        switch_view = f'<p>First-seen reconstruction through {stamp}. Times describe capture. Counts cover this archive page.</p>'
        if older_bytes or older_seq:
            switch_view += f'<a href="?one_clock_at={cutoff}&amp;end={older_bytes}&amp;seq={older_seq}">Read older archive page</a>'
        switch_view += ' <a href="/credit-tape/">Return to live tape</a>'
    notices = len(events) - len(rows)
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Credit tape · Options View</title>
<link rel="stylesheet" href="/credit-tape/table.css"><script defer src="/credit-tape/table.js"></script></head>
<body><main data-cursor="{h(cursor)}" data-mode="{mode}" data-publisher-filter="{'Tradeweb' if treu_view else ''}"><header><a href="/">Options View</a><h1>{title}</h1>{switch_view}
<p>{len(rows)} reports · {notices} archive notices in the current window</p>
<small>Select an instrument for details. Tradeweb includes unclassified instruments. First seen in Pacific time. K = thousand, M = million, B = billion. + marks a capped lower bound.</small></header>
<nav aria-label="Time index">{links}</nav>
<section hidden id="controls"><input type="search" id="q" aria-label="Search prints" placeholder="Name, product, expiry, amount or ID">
<label>Publisher <select id="pub"><option value="">All</option></select></label>
<label>Channel <select id="ch"><option value="">All</option></select></label>
<label>Action <select id="act"><option value="">All</option></select></label><output id="count"></output></section>
<details hidden id="columns"><summary>Optional columns</summary>
<fieldset><legend>Show in the table</legend>
<label><input type="checkbox" value="Execution">Execution time</label>
<label><input type="checkbox" value="Publication">Publication time</label>
<label><input type="checkbox" value="Price">Price</label>
<label><input type="checkbox" value="Action">Action</label>
<label><input type="checkbox" value="Report ID">Report ID</label></fieldset></details>
<p id="status" role="status"></p><div class="table-wrap"><table><colgroup><col class="time"><col><col class="action"><col class="amt"></colgroup>
<thead><tr><th>Seen PT</th><th>Instrument / expiry</th><th>Source</th><th>Notional</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div><button id="more" type="button" hidden>Show more reports</button><form method="post" action="/credit-tape/auth/logout"><button>Sign out of credit tape</button></form></main></body></html>'''.encode()
