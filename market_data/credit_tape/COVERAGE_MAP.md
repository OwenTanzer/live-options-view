# Credit source coverage map beyond DTCC (OA-242)

Documentation and existing evidence only. No outreach, subscriptions, polling, terms
acceptance or provider changes were made.

- **Desk research** was on 2026-11-20, using WebSearch and TinyFish. The project's
  SearXNG search was unavailable: its Docker stack was stopped.
- **Live public pages:** only one was opened, the KOR CFTC ticker's credit view, read
  once. No data was retrieved or stored.
- **Labels:** **Official** = the operator's or regulator's own document says it.
  **Observed** = seen directly on a public page or in a public document. **Not
  verified** = no evidence found yet.

## U.S. — CFTC swap data repositories (Part 43) and SEC SBSDRs (Regulation SBSR)

Under CFTC Part 43 and Regulation SBSR, a trade is publicly disseminated by the SDR
or SBSDR it is reported to. A credit trade appears on DTCC's tape only if it was
reported to DTCC. Trades reported to another SDR appear only on that SDR's public
ticker.

### CME Repository Services (CME SDR)

Paper check: [CME_CREDIT_ROUTE.md](CME_CREDIT_ROUTE.md). The credit files are daily (reconciliation-only), there is no documented intraday machine-readable surface, and CME's Data Terms prohibit automated access without written permission.

- **Status:** CFTC-registered SDR for Credit, Rates, FX, Commodity and Equity.
  (Official: [CME repository page](https://www.cmegroup.com/market-data/repository/data.html).)
  CME closed its EU and Australian repositories but kept the U.S. SDR (press reports).
- **Index vs single-name:** not documented separately. A "Credit" tab exists. SEC
  security-based swaps (single names, narrow indices) are not covered: CME is not
  an SBSDR.
- **Public access:** intraday. Data is "pushed to CMEGroup.com immediately upon
  receipt", subject to regulatory delays, with a download per asset class as dated
  files (Official). The page says the data is "intended for direct public use", and
  vendors or firms redistributing it must contact CME (Official).
- **Credit volume:** **not verified, and likely small.** CME exited CDS clearing in
  2018 and transferred open positions to ICE Clear Credit
  ([CME press release 2017-09-14](https://www.cmegroup.com/media-room/press-releases/2017/9/14/cme_group_to_exitotccreditclearingbusinessreturn650mincapitaltoc.html);
  [advisory 17-491](https://www.cmegroup.com/notices/clearing/2017/12/Chadv17-491A.pdf)).

### ICE Trade Vault (ICE SDR; also an SEC SBSDR)

Paper check: [ICE_TRADE_VAULT_ROUTE.md](ICE_TRADE_VAULT_ROUTE.md). ICE is parked.

- **Status:** CFTC SDR. Also approved as an SEC SBSDR in June 2021, covering credit.
- **Index vs single-name:** CFTC side: "all publicly reportable swap transactions
  within the Credit, Foreign Exchange, Interest Rates and Other Commodities asset
  classes" (Official). SEC side: security-based swaps in credit, i.e. single names
  and narrow indices. The SEC ticker URL was **not found**.
- **Public access:** intraday, with a gate. The ICE Real-Time Ticker
  (`https://tradevault.ice.com/tvcftc/ticker/`) auto-refreshes "immediately upon
  reception of valid Part 43 messages". Historical tick reports can be downloaded by
  date. **Users must accept terms of use** before accessing current or historical
  data. (Official: [Real-Time Ticker Regulatory Guide, Dec 2022](https://www.ice.com/publicdocs/data/ICE_Trade_Vault_Real-Time_Ticker_Regulatory_Guide.pdf).)
- **Credit volume:** **not verified.** Terms acceptance is a decision for Owen, so
  the ticker was not opened. The public ICE CDS documents reviewed do not say which
  SDR receives cleared CDS reports.

### KOR Reporting (KOR SDR; also an SEC SBSDR)

Documentation check: [KOR_CFTC_API_CHECK.md](KOR_CFTC_API_CHECK.md) (schema, filter/cursor semantics, access rules, evidence gap).

- **Status:** CFTC SDR, provisionally registered on 2022-03-30, licensed "in all
  asset classes". SEC SBSDR approved by order dated 2024-11-04
  ([89 FR 2024-25961](https://www.govinfo.gov/content/pkg/FR-2024-11-08/html/2024-25961.htm)).
- **Index vs single-name:** the CFTC ticker has a Credit tab, but index vs
  single-name is not documented. On the SEC side, SBS credit is single names and
  narrow indices. **No public SEC ticker link was found** on KOR's site, which links
  only CFTC, Canada and ASIC public pages.
- **Public access:** intraday, documented API.
  - The public ticker UI is at `https://public.cftc.korfinancial.com/ticker?assetClass=CR`.
  - The Ticker API at `https://ticker.cftc.korfinancial.com/ticks` is documented
    with an OpenAPI spec. It supports filtering by asset class and cursor
    pagination. The example request shows no authentication, and items carry
    `disseminationIdentifier`, `actionType`, `eventType`, notional, price and dates
    ([KOR public-pages guide](https://public.cftc.korfinancial.com/assets/query-data-on-the-public-pages.pdf)).
- **Credit volume:** **not verified.** At the single read on 2026-11-20 the credit
  view showed "No data available" (Observed). KOR's Acceptable Use Policy could not
  be retrieved, so the terms for automated retention are **not verified**.

## UK — post-trade transparency under UK MiFIR (FCA PS24/14, in force 1 Dec 2025)

**Rules** (Official: [FCA PS24/14](https://www.fca.org.uk/publication/policy/ps24-14.pdf)):

- **Index:** iTraxx Europe Main and iTraxx Europe Crossover with a 5-year tenor
  are **Category 1** instruments (¶3.31). They are among the most liquid credit
  indices alongside CDX.NA.IG/HY, and qualify by virtue of the derivatives trading
  obligation (¶5.38–5.39).
  - Category 1 trades are published in real time, unless above the large-in-scale
    thresholds. Above-threshold trades get price at end of day and size at the end
    of the following quarter.
  - Thresholds: iTraxx Main £50m / £70m; Crossover £15m / £20m (rules annex, p.132).
  - CDX is not in the UK Category 1 list.
- **Single-name:** single-name CDS were deliberately **not** made Category 1
  (¶3.18, ¶3.21). Investment firms trading Category 2 instruments OTC are not
  required to publish post-trade reports, so UK single-name coverage is expected to
  be thin.
- **Free delayed data:** trading venues must make post-trade data "free of charge
  15 minutes after publication" (MAR 9A.2.1R(2), p.117). The equivalent APA
  obligation in UK MiFIR was confirmed only through search excerpts, not read in
  full.

**Publishers:**

| Publisher | Coverage (documented) | Public access | Gated |
|---|---|---|---|
| Tradeweb UK APA (TREA) and UK MTF (TREU) | Post-trade data from Tradeweb's UK APA and venues; Tradeweb lists credit default swaps among its markets | **Delayed data website `https://www.apa.tradeweb.com/`**: at least 15 minutes delayed, "human and machine-readable", with a CSV download; listed in Tradeweb's fee schedule with no fee (Official: [APA page](https://www.tradeweb.com/our-markets/data-analytics/trade-reporting-services-apa), [fee schedule Aug 2026](https://www.tradeweb.com/4a4d3a/globalassets/disclosures/08.17.26/mifid-ii-transparency-market-data--annex-b--fee-schedule--version-2.0_august-2026.pdf)) | Real time (under 15 minutes) is licensed: £13,200 per firm per data set per year ([UK APA/MTF fee schedule](https://www.tradeweb.com/4a3842/globalassets/disclosures/08.17.26/current-docs/market-data-policy-and-fee-schedule---tradeweb-europe-limited---uk-apa-and-uk-mtf.pdf)); the real-time site needs a login |
| Bloomberg APA UK (BDRSL) and Bloomberg venues | Derivatives "IR/Credit/FX/Commodity/Equity" | Free delayed page **not confirmed** | Real time via a FIX feed (licensed) |
| MarketAxess (Trax) APA; TP ICAP, BGC, GFI, Tradition venues | **Not verified** for UK credit derivatives | **Not verified** | — |
| UK derivatives consolidated tape | None: the FCA has not stated an intention for derivatives (Tradeweb blog, Dec 2025) | — | — |

LSEG alternative: [LSEG_UK_DELAYED_CHECK.md](LSEG_UK_DELAYED_CHECK.md). It is not a confirmed alternative.

Bloomberg UK MTF (BMTF) on-venue complement: [BMTF_UK_CHECK.md](BMTF_UK_CHECK.md). It trades iTraxx Main/Crossover, but publication of those rows in its delayed CSV feed and its Terms of Use are unverified.

## Candidate for the next implementation step

Follow-up: [TRADEWEB_UK_GO_NO_GO.md](TRADEWEB_UK_GO_NO_GO.md) has the datasets, fields, cadence and terms, with the verdict and the named gaps.

**Tradeweb's free UK delayed-data website (`apa.tradeweb.com`), for iTraxx Europe
Main and Crossover (5y) prints.**

- **Why this one:** it is the only source found that is documented as **free,
  intraday (at least 15 minutes delayed) and machine-readable** for the empty UK
  lane. Category 1 status since 1 Dec 2025 means these index trades must be
  published in near real time, apart from large-in-scale deferrals.
- **Still missing before any build:**
  1. **Evidence that iTraxx prints appear there**, and in which fields (instrument
     identifiers, price convention, deferral flags), confirmed from Tradeweb
     documentation rather than by polling. Not verified.
  2. **Terms of use** for automated retrieval, private retention and internal
     display. A decision for Owen, like the DTCC terms question.
  3. **Coverage limits:** this covers only trades published through Tradeweb's APA
     and venues. Bloomberg, MarketAxess and other venues would each be separate
     sources, and there is no UK derivatives consolidated tape. Single-name UK CDS
     are outside Category 1, so expect little or nothing.

**U.S. alternatives:**
- KOR's documented public Ticker API, which needs no login, is technically the
  easiest addition. But credit volume there is unproven (empty when read), and its
  terms are unread.
- ICE's ticker needs terms acceptance before even inspecting it.
- CME credit is likely negligible after its 2018 CDS-clearing exit.

None of the three replaces DTCC for U.S. credit, and none has evidence yet of
material credit volume.

Fresh SEC proof and the DTCC pilot remain separate.
