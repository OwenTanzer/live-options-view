"""Authenticated Options View credit-tape table."""
from datetime import datetime
from decimal import Decimal, InvalidOperation
from html import escape
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


def render_table(events, cursor, has_sample=False, sample_view=False, has_treu=False, treu_view=False):
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
        if treu_view:
            filename, _, digest = ev["raw_ref"].partition("#")
            raw_link = (f'<small><a href="?view=treu&amp;file={h(filename)}&amp;sha={h(digest)}">'
                        f'Original CSV · row {int(ev["row_index"])+1}</a></small>')
            detail = " · ".join(str(v) for v in (detail, ev.get("id"),
                               "Executed " + str(ev.get("execution_ts") or "unknown"),
                               "Published " + str(ev.get("dissemination_ts") or "unknown")) if v)
        action = ev.get("action") or "—"
        channel = (ev.get("jurisdiction") or ev.get("source") or "—").removeprefix("US-")
        publisher = ev.get("publisher") or ev.get("source") or "Unknown publisher"
        if publisher == "DTCC Data Repository (U.S.) LLC":
            publisher = "DTCC"
        venue = ev.get("channel") if channel == "UK" else ""
        origin = " · ".join(str(v) for v in (publisher, channel, venue) if v)
        history = " · ".join(k for k in ("backfill", "replay") if ev.get(k))
        index = " ".join(str(v) for v in [*s.values(), ev.get("id"), ev.get("original_id"), ev.get("first_seen")] if v)
        rows.append(f'<tr{anchor} data-publisher="{h(publisher)}" data-search="{h(index)}" data-channel="{h(channel)}" data-action="{h(action)}">'
                    f'<td>{stamp}</td><td>{h(name)}<small>{h(detail)}</small>{raw_link}</td>'
                    f'<td>{h(action)}<small>{h(origin)} {h(history)} {h(ev.get("qualification"))}</small></td>'
                    f'<td><b>{h(amount(s.get("notionalAmountLeg1")))}</b><small>{h(s.get("notionalCurrencyLeg1"))}</small></td></tr>')
    links = " ".join(f'<a href="#t{k}">{v}</a>' for k, v in anchors.items())
    switch_view = ""
    if has_sample:
        switch_view = ('<a href="/credit-tape/">Return to live tape</a>' if sample_view else
                       '<a href="/credit-tape/?view=treu-sample">Tradeweb UK sample</a>')
    title = "Tradeweb UK sample" if sample_view else "Credit tape"
    if has_treu:
        if treu_view:
            title = "Tradeweb UK MTF · delayed intraday reports"
            switch_view = '<a href="/credit-tape/">DTCC credit tape</a>'
        else:
            switch_view += ' <a href="/credit-tape/?view=treu">Tradeweb UK MTF · delayed intraday reports</a>'
    notices = len(events) - len(rows)
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Credit tape · Options View</title>
<link rel="stylesheet" href="/credit-tape/table.css"><script defer src="/credit-tape/table.js"></script></head>
<body><main data-cursor="{int(cursor)}"><header><a href="/">Options View</a><h1>{title}</h1>{switch_view}
<p>{len(rows)} reports · {notices} archive notices in the latest {len(events)} events</p>
<small>First seen in Pacific time. K = thousand, M = million, B = billion. + marks a capped lower bound.</small></header>
<nav aria-label="Time index">{links}</nav>
<section hidden id="controls"><input type="search" id="q" aria-label="Search prints" placeholder="Name, product, expiry, amount or ID">
<label>Publisher <select id="pub"><option value="">All</option></select></label>
<label>Channel <select id="ch"><option value="">All</option></select></label>
<label>Action <select id="act"><option value="">All</option></select></label><output id="count"></output></section>
<p id="status" role="status"></p><table><colgroup><col class="time"><col><col class="action"><col class="amt"></colgroup>
<thead><tr><th>Seen PT</th><th>Instrument / expiry</th><th>Action</th><th>Notional</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table><form method="post" action="/credit-tape/auth/logout"><button>Sign out of credit tape</button></form></main></body></html>'''.encode()
