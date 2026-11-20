# Bloomberg UK MTF (BMTF) delayed post-trade route for iTraxx Europe Main/Crossover 5y (OA-242)

Read on 2026-11-20. Search-indexed text and public documents only, through search
engines. Bloomberg's sites (`data.bloombergmtf.com`, `assets.bbhub.io`,
`professional.bloomberg.com`, `bloomberg.com`) were **not** fetched directly. No
download, polling, sign-up, terms acceptance, contact or implementation.

Scope: Bloomberg Trading Facility Limited's UK MTF (**BMTF**, FCA-authorised) as an
on-venue complement. Bloomberg's EU venue (BTFE, Bloomberg Trading Facility B.V.) and
Bloomberg's APA are out of scope. BTFE documents are cited only where they are labelled
as such.

Labels: **Official** (Bloomberg's own text, as indexed), **Regulatory**,
**Indexed third-party**, **Gap**.

## Verdict: trades CDS, publication of Main/Crossover in the feed is unproven

BMTF **trades** iTraxx Main and Crossover. There is a free, 15-minute-delayed
transparency page with CSV files. Nothing I could read shows that those files actually
contain iTraxx Main or Crossover 5y rows. The governing Terms of Use text was not
indexed, and the fee disclosure limits free delayed data to "internal proprietary
purposes". Keep BMTF as a candidate, not a route.

## 1. Does BMTF trade these contracts? Yes (Official)

- **Official:** the BMTF product page describes BMTF as "a robust trading platform for
  trading cash bonds, repos, credit default swaps ('CDS'), interest rate swaps".
- **Official:** the BMTF rulebook (current version effective 17 April 2026) lists credit
  default swaps under its RFQ and RFT functions (Annex A).
- **Official:** the BMTF rate card ("Effective 01 April 2025 – Bloomberg Multilateral
  Trading Facility") has per-index fee lines: "Index ITRAXX Cross Over. 0.0163 x DV01.
  Index ITRAXX Main. 0.0065 x DV01", plus Senior Financial and Subordinate Financial.
  This proves the venue charges for iTraxx Main and Crossover executions. It does **not**
  prove tenor (5y) or that trades occurred in any given window.
- **Regulatory:** iTraxx Europe Main and Crossover 5y are Category 1 under FCA PS24/14
  (in force 1 Dec 2025), as recorded in [COVERAGE_MAP.md](COVERAGE_MAP.md).

## 2. Delayed post-trade route (Official, as indexed)

- **Surface:** `https://data.bloombergmtf.com/transparency` ("Transparency Data"). The
  home page snippet reads: "Pre/post-trade Transparency Data made available on this page
  are subject to the Terms of Use linked below (the 'ToS')". The page also carries a
  carve-out: a ToS provision "held inconsistent with the obligations applicable" under
  MiFIR does not apply.
- **Delay and cost:** the BTF Market Data Fees Disclosure 2026 says rights to data
  published "15 minutes after its publication ('Delayed Data') are made available at no
  charge provided that the recipient uses the Delayed Data only for internal proprietary
  purposes."
- **Regulatory:** Bloomberg's 2021 Ontario Securities Commission application for
  BTFL describes pre- and post-trade data made available to the public free of charge
  15 minutes after publication at that transparency URL.
- **Format:** BMTF Market Notice MN-23-02 says that from 24 July 2023 "'Terms of Use'
  text [is] inserted at the top of each csv file". The text is "contained within the top
  12 rows of the csv file". So the delivery is CSV, with a 12-row ToS header to skip.
- **Gap:** cadence (intraday files vs one daily file), file naming, columns, and whether
  a listing endpoint exists are not documented in anything indexed.
- **Deferrals:** the rulebook says deferral-eligible trade data is withheld until the
  deferral period expires, and that deferrals apply automatically. So a Main/Crossover
  print may appear later than 15 minutes, and possibly with a capped size.

## 3. Evidence that the feed carries 5y Main and Crossover: none found (Gap)

- No indexed CSV sample, column list, ISIN list or instrument filter for the BMTF
  transparency files.
- Indexed third-party: Clarus's "Bloomberg MTF Data – Week One" (2018, pre-Brexit, so it
  describes the old EU-era BMTF) found weekly aggregated data for about 1,400 ISINs, of
  which "only 33 were Derivatives". That is old, and it is about aggregation, not the
  current UK per-trade files. It is a warning, not evidence either way.
- The two kinds of evidence stay separate: "trades CDS" is Official (rate card, rulebook).
  "Publishes these contracts in that feed" is unproven.

## 4. Terms for automated collection, retention and private display

- **Gap:** the BMTF transparency Terms of Use text itself was not indexed. I did not
  open it, because reading it on the site is the only way to see it, and the site's
  automation terms are unknown.
- **Official (fee disclosure 2026):** free only for "internal proprietary purposes". Our
  GitHub-gated panel for named collaborators looks like internal use, but the terms do
  not define it. Non-internal use needs approval (BTFE disclosure language; the BTFL
  equivalent is not indexed).
- **Official (BTFE, EU, not BMTF):** the BTFE market data policy (23 Aug 2026) says other
  uses "are not permitted" and restricts combining the data with other data to create
  new products. If BMTF's ToS mirrors this, combining it with the DTCC tape in one panel
  needs a reading.
- **Official (BMTF rulebook):** the operator owns "all rights, title and interest,
  database rights" in MTF data, subject to participants' rights in their own data.
- **Indexed third-party (secondhand):** Bloomberg's general bloomberg.com Terms of
  Service are quoted as barring scrapers, bots and spiders without written consent. I
  could not confirm the wording on Bloomberg's own page, or whether those terms reach
  `data.bloombergmtf.com`. The MTF page names its own ToS, so that text governs.

## Exactly what is missing

1. A BMTF transparency CSV (or official spec) showing iTraxx Europe Main and Crossover
   5y rows, with tenor, size field and deferral flags.
2. The file cadence and naming (intraday vs end-of-day), so we know the publication time.
3. The `data.bloombergmtf.com` Terms of Use text: automated collection, retention, and
   whether internal display to named collaborators is "internal proprietary".
4. Whether combining BMTF rows with other sources is restricted, as it is for BTFE.

Items 1–3 need someone to open the page as a human (Owen's call on acceptance). No
automated read of Bloomberg sites was made, and none should be made until item 3 is read.

## Sources (as indexed)

- BMTF product page: https://professional.bloomberg.com/products/trading/electronic-markets/venues/bmtf/
- BMTF transparency page: https://data.bloombergmtf.com/transparency
- BMTF Rulebook 17 Apr 2026: https://assets.bbhub.io/professional/sites/27/BMTF-Rulebook-Effective-17-APR-26.pdf
- BMTF rate card (iTraxx lines): https://assets.bbhub.io/professional/sites/27/BMTF-Rate-card_FIT_FX.pdf
- BTF Market Data Fees Disclosure 2026: https://assets.bbhub.io/professional/sites/27/BTFL-Market-Data-Fees-Disclosure-2026.pdf
- BMTF Market Notice MN-23-02: https://assets.bbhub.io/professional/sites/10/BMTF-Market-Notice-MN-23-02.pdf
- OSC application (2021): https://www.osc.ca/sites/default/files/2021-05/bloomberg_20210513_trading-facility-limited-application-3_0.pdf
- BTFE market data policy 23 Aug 2026 (EU, comparison only): https://assets.bbhub.io/professional/sites/27/BTFE-market-data-policy_23-August-2026.pdf
- Clarus, "Bloomberg MTF Data – Week One": https://clarusft.com/bloomberg-mtf-data-week-one
