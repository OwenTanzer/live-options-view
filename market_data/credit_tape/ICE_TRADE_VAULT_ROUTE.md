# ICE Trade Vault public credit route: paper check (OA-242)

Read on 2026-11-20 from published guides and regulator filings only.
- No gated ticker was opened, no terms accepted, no data downloaded, no polling, no
  contact with ICE.
- **Disclosure:** ICE's website terms prohibit "spiders, robots … tools" and "data
  mining, robots or similar" methods unless ICE expressly permits them. In this
  session I made a few automated ice.com reads: the terms page, the Dec 2022 ticker
  guide, and two ICE CDS PDFs. After reading the terms, I used only CFTC-hosted
  copies and an SEC-filed copy.

## CFTC Credit (ICE SDR, Part 43)

- **Rulebook:** CFTC-filed ICE Trade Vault SDR Rulebook, last updated 28 Feb 2024
  ([cftc.gov rules03062412455.pdf](https://www.cftc.gov/filings/orgrules/rules03062412455.pdf)),
  p.18–19. Data is "free of charge"; instructions on "how to download, save and
  search the data"; available "through the ICE Real-Time Ticker for a period of one
  year".
- **Ticker functionality**, per the rulebook:
  - "Dissemination of changes, corrections, and cancellations";
  - "Ability to request and replay messages for a given day";
  - "Ability for the public … to download historical data".
- **Asset classes:** credit, commodities, rates and FX are accepted.
- **Ticker guide** (Dec 2022, read earlier):
  - the ticker is at `tradevault.ice.com/tvcftc/ticker`;
  - **users must accept terms of use** before seeing current or historical data;
  - historical tick reports are downloadable by date;
  - no API is described.
- **Gaps:**
  - the replay and history mechanics (who can use them, format, identifiers) are not
    described publicly;
  - the terms behind the gate are unread;
  - credit volume is unknown.

## SEC security-based Credit (ICE SBSDR, Reg SBSR)

- **Guide:** SEC-filed Exhibit N.4, *ICE SBSDR Public Dissemination Regulatory
  Guide*, last updated 31 Jan 2021, Form SDR application.
  - Disseminates "all publicly reportable Security-based swaps within the Credit
    asset class", under no-action relief that applies CFTC rules.
  - Notional is capped at "$5MM+" for single-name and narrow-index trades.
  - "Cancels & Errors are ticked immediately."
- **Access:**
  - "Data will be displayed on a GUI **which may be scraped** and is also available in
    a CSV export." Data "remains publicly and freely available".
  - "No login access or executed agreements are required" (via icetradevault.com,
    Menu → SBSDR Data Reports, "top day" or historical).
  - CSV export of the current day, and of history over "a date range up to 31 days".
- **Fields:** listed in Exhibit N.5, which was not obtained.
- **Exclusions:** clearing SBS and post-allocation SBS.
- **Gaps:**
  - the 2021 guide predates SBSR going live in Feb 2022; the current version on
    ice.com was not read, so whether "may be scraped" and no-login still hold is
    unverified;
  - no API or feed is documented;
  - no record key or sequence is documented for de-duplication;
  - credit volume is unknown.

## Verdict

- **Machine-readable route:** neither side documents an API or feed. The SEC side
  documents a scrape-permitted GUI plus manual CSV export. The CFTC side has a
  terms-gated GUI with daily history download and per-day "replay".
- **Recovery:** SEC side, historical export of up to 31 days. CFTC side, one-year
  retention with download and replay of a given day. Neither method's mechanics are
  public.
- **Exact remaining gap:**
  1. the current (post-2022) SEC dissemination guide and Exhibit N.5 field list;
  2. the CFTC ticker's terms of use behind the gate (Owen's decision);
  3. a documented record identifier and replay format, needed for loss-free
     recovery;
  4. evidence of actual credit volume on either side.
