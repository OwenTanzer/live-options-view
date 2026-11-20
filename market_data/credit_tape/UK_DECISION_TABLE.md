# UK credit routes: ranked decision table (Tradeweb, ECHO, BMTF) — OA-242

Desk-only, 2026-11-20. Built only from the existing screens:
[TRADEWEB_UK_GO_NO_GO.md](TRADEWEB_UK_GO_NO_GO.md),
[LSEG_UK_DELAYED_CHECK.md](LSEG_UK_DELAYED_CHECK.md) (ECHO) and
[BMTF_UK_CHECK.md](BMTF_UK_CHECK.md). There was no new fetch, download, polling,
signup, terms acceptance, contact or implementation. The U.S. DTCC pilot is separate.

Target: actual iTraxx Europe Main and Crossover **5y** prints (UK Category 1 under
FCA PS24/14). Single-name CDS are outside Category 1, so they do not drive the ranking.

## Venue trades vs OTC reports

- **Venue trades (MTF):** trades executed on the venue are published by that venue.
  Tradeweb UK MTF (TREU) and Bloomberg UK MTF (BMTF) are here. Each covers only its
  own venue.
- **OTC reports (APA):** UK investment firms' off-venue Category 1 trades are published
  through whichever APA the firm chooses. Tradeweb UK APA (TREA) and TRADEcho UK APA
  (ECHO) are here. Each covers only the firms that report through it.
- No single route covers the UK market, and there is no UK derivatives consolidated
  tape. Any route is partial by construction.

## Ranked table

| Rank | Route | Type | Product evidence for Main/XO 5y | Release timing | Terms | The one missing fact that would change the decision |
|---|---|---|---|---|---|---|
| 1 | **Tradeweb UK MTF (TREU)** post-trade delayed | Venue trades | **Partial (Documented):** the UK/EU MTF fee schedule lists "EU CDS Index" with **EU Main** and **Xover**. No tenor, and UK and EU are combined. The 2025 disclosure shows 691 credit-derivative instruments on the UK MTF (no index/tenor split) | At least 15 min after publication; "delivered continuously"; same release to all channels. LIS deferral: price end of day, size end of the following quarter | **Readable in full.** Free for "own use". Accepted by download. Automation and retention not addressed. "Aggregating … or creating historical series" is value-added, chargeable "where you charge fees" (ambiguous for a non-fee private tape) | Whether the free TREU file actually carries Main/XO **5y** rows, and how to identify them (ISIN only) |
| 2 | **Bloomberg UK MTF (BMTF)** transparency CSV | Venue trades | **Partial (Official):** the rate card has fee lines "Index ITRAXX Main" and "Index ITRAXX Cross Over". No tenor, and no proof of trades in any window | 15 min delayed CSV (12-row ToS header). Deferrals apply automatically. Cadence and naming undocumented | **Not readable:** the transparency ToS text is not indexed. The fee disclosure allows free use "only for internal proprietary purposes". The EU sister venue's policy bans other uses and combining data | The `data.bloombergmtf.com` ToS text (it gates whether BMTF can be considered at all) |
| 3 | **Tradeweb UK APA (TREA)** post-trade delayed | OTC reports | **None specific:** 7,095 credit-derivative instruments reported in 2025, about €27.7bn ADT, no index/tenor split. Which firms report iTraxx via TREA is not public | Same as TREU | Same document as TREU | Whether any UK firm's OTC Main/XO 5y trades are published in TREA |
| 4 | **TRADEcho UK APA (ECHO)** on LSEG's delayed portal | OTC reports | **None specific:** the FIX spec accepts "Credit Derivatives" reports. No instrument or turnover disclosure | Semicolon-delimited UTC minute files, 15 min after first publication (Chej's correction from LSEG's own page) | **Restrictive as indexed:** LSEG website terms ban automation "regardless of … internal" use. Portal terms name certain paid uses that need a licence. Whether the website ban reaches the portal is unestablished | Whether LSEG's automation ban applies to `dmd.lseg.com` (if it does, ECHO is out for an unattended collector, whatever it carries) |

### Why this order

1. **TREU first:** it is the only route with documented iTraxx Main/XO product lines
   **and** fully readable delayed-data terms. Its blockers are a coverage fact and a
   licence judgement, not unknown text.
2. **BMTF second:** product evidence is as good as TREU's, but the governing terms text
   is unseen and the indexed use limit ("internal proprietary") is narrower.
3. **TREA third:** same readable terms as TREU and a large credit footprint, but no
   evidence that iTraxx OTC reports go through it. It can share TREU's check for free
   if the same file set covers both.
4. **ECHO last:** no product evidence, and the only terms text indexed points against
   unattended collection.

## Recommended next check (smallest single step)

**One manual look at one Tradeweb UK MTF (TREU) post-trade delayed file, done by a
person after Owen accepts Tradeweb's UK Delayed Data Terms (last updated 17 Aug 2026).**

- **Who and how:** Owen, or Jayden with Owen's OK, opens `apa.tradeweb.com` in a
  browser once. No script, no polling, no account.
- **Scope:** the TREU post-trade file(s) for **one** recent UK business day, plus the
  matching TREA file only if it sits in the same place.
- **What to record:** column names, file cadence and naming, and whether any row is
  identifiable as iTraxx Europe Main or Crossover 5y (by ISIN against the current
  on-the-run series, with tenor, notional, price convention and deferral flags).
  Keep the file private and local, out of the repo.
- **Stop conditions (any one ends the check, report and don't escalate):**
  1. The download asks for registration, credentials or terms other than the
     17 Aug 2026 Delayed Data Terms.
  2. No row in that day's file can be identified as Main or Crossover 5y, either
     because none exist or because identification needs a second paid or registered
     reference source. Report "no evidence for that day". Don't try more days.
  3. Owen declines acceptance-by-download, in which case Tradeweb stays parked and
     BMTF's ToS text becomes the next item for a human read.
- **What it decides:** if Main/XO 5y rows are present and identifiable, Tradeweb TREU
  becomes the UK candidate. The remaining question is then the licence judgement on a
  non-fee private tape (Owen). If not, the UK lane has no evidenced route yet.

No other broad publisher screens are proposed.
