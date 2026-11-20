# One-day identification key: iTraxx Europe Main and Crossover 5y in UK post-trade rows (OA-242)

Desk-only, 2026-11-20. Prepared for the single TREU business-day check proposed in
[UK_DECISION_TABLE.md](UK_DECISION_TABLE.md), which is pending Owen's approval. It uses
**no Tradeweb data**, and involved no terms acceptance, outreach or implementation.
Sources are official product and regulatory references, read as search-indexed text.
S&P Global, FCA and ANNA DSB pages were **not** fetched directly; S&P's site and
product terms restrict automated access.

Labels: **Official** (index sponsor or regulator text, as indexed), **Derived**
(follows from an official rule but not stated for this series), **Gap**.

## 1. Which contracts the key covers

Valid for trading days from the Series 46 roll (21 Sep 2026) until the Series 47 roll
(expected around 20 Mar 2027). After that, on-the-run moves to S47 and S46 becomes
first off-the-run, so the key must be rebuilt.

| Contract | Role | Index RED code | Series / version | Roll date | Scheduled 5y maturity |
|---|---|---|---|---|---|
| iTraxx Europe (Main) 5y | on-the-run | **2I666VDO0** (Official: S&P S46 annex and Sep 2026 RED list) | 46 / 1 | 21 Sep 2026 (Official) | 20 Dec 2031 (**Derived**: the ICE contract spec says a 20 September roll gives a 20 December maturity) |
| iTraxx Europe (Main) 5y | first off-the-run | **2I666VDN2** (Official: S&P RED list) | 45 / 1 | 20 Mar 2026 | 20 Jun 2031 (**Derived** from the same rule; consistent with listed products referencing "iTraxx Europe Series 45 5Y", final maturity 20/06/2031) |
| iTraxx Europe Crossover 5y | on-the-run | **2I667KND6** (Official: S&P RED code list) | 46 / 1 | 21 Sep 2026 | 20 Dec 2031 (**Derived**) |
| iTraxx Europe Crossover 5y | first off-the-run | **2I667KNC8** (Official: S&P S45 annex and RED lists) | 45 / 1 | 20 Mar 2026 (annex 17 Mar 2026) | 20 Jun 2031 (**Derived**) |

- **Versions:** Crossover is re-versioned after a credit event (for example, S43 traded
  as Version 2). No Version 2 of Main or Crossover S45/S46 was found in indexed sources.
  That is search-only, not proof. **Any version other than the one above is undecided**
  until it appears in an official RED list.
- **Not in the key:** other tenors (3y, 7y, 10y), older series (S44 and below),
  tranches, options, and iTraxx Senior Financials, Sub Financials and Non-Financials.
  All stay unmapped.

## 2. Which published fields establish tenor and series

The UK public record (FCA MAR 11 Annex 2, Table 2, Official) carries only:
- instrument identification code: **"{UPI}; or {ISIN}"**. PS24/14 says to report only
  the UPI where one exists (OTC derivatives), otherwise the ISIN;
- trading date and time;
- price, price notation and currency;
- quantity or notional (omitted under VOLO);
- venue of execution (MIC, or `XOFF` for OTC);
- publication date and time;
- venue of publication (MIC);
- transaction identification code;
- flags.

**None of these public fields states index family, series, version or tenor.** Tradeweb's
Annex B also says the free delayed file contains "the prescribed post-trade
transparency fields and no other field". So:

- **Series, version and tenor can only come from the reference record behind the
  identifier.** For an **OTC ISIN**, the ANNA DSB record carries the underlying credit
  index, index series, index version and term, plus the maturity (Official, DSB
  guidance: series and version are part of OTC ISIN granularity).
- **A UPI is coarser.** DSB material does not confirm that the UPI carries series,
  version or term. **Gap:** until the DSB UPI definition for credit index CDS is read,
  a row identified by UPI alone cannot establish series or tenor, so it stays undecided.
- **A row is mapped to a contract only when all of these match one key row:**
  1. Underlying index RED code (or DSB index name plus series plus version).
  2. Term of 5 years, **and** maturity date equal to the scheduled maturity above. The
     maturity is the decisive check, because "5Y" alone does not separate S45 from S46.
  3. Currency EUR (Main and Crossover trade in EUR).
- **Gap:** reading the DSB record needs DSB access. Its web lookup is account-based,
  which counts as signup, so it is not done here. That puts it under the stop condition
  in UK_DECISION_TABLE.md.

## 3. When a row must stay undecided

A row is **undecided** (never counted as Main/XO 5y, and never dropped silently) when:

1. its identifier cannot be resolved to a reference record, for any reason;
2. it is a UPI whose reference data lacks series, version or term;
3. it resolves to the right index and series but a different term or maturity, or to a
   version not in the key;
4. price notation or currency contradicts the key (for example, a non-EUR notional on
   a Main/XO identifier);
5. it is a FULV row whose transaction identification code has no matching VOLO row on
   the day checked. Its original trade may sit on an earlier day, which is outside the
   one-day scope.

Undecided rows are listed with their raw identifier and the reason.

## 4. Linking VOLO and FULV without double counting

- **Official (MAR 11.5):** where a transaction qualifies for a volume deferral, the
  firm "must use the VOLO flag for the first trade report, omitting the relevant
  details, and use the FULV flag for the full trade report once it is published."
  - `VOLO`: "Transaction for which limited details are published".
  - `FULV`: "Transaction for which limited details have been previously published".
  - PS24/14 deleted all other supplementary deferral flags.
- **Link key:** (venue of publication MIC, transaction identification code). The TIC is
  assigned by the trading venue or APA, so it is unique only within one publisher. The
  MIC must be part of the key.
- **Count rule:** one trade per link key, dated by its **original trading date and
  time** (not the publication time). The VOLO row is the first sighting (first-seen and
  publication time recorded). The FULV row supplies the omitted volume and is
  **not** a second trade.
- **Keep both rows** in the raw record. Record the VOLO and FULV publication times
  separately, so publisher deferral is kept apart from our observation delay.
- **Gap:** cancellation and amendment flags (if present in the UK Table 3 list) would
  also join on the same link key. Their handling is not specified here, so a
  cancelled or amended TIC stays undecided.

## 5. Fields recorded per row in the one-day check

publication MIC (venue of publication), venue of execution MIC, original trading date
and time, publication date and time, transaction identification code, identifier type
(UPI or ISIN) and value, flags, price and notation, notional and currency (when not
omitted), the key row matched (or "undecided" plus the reason), and the VOLO/FULV link
status.

## Sources (as indexed)

- S&P Global, iTraxx Europe Series 46 Version 1 annex (Main RED 2I666VDO0, roll
  21 Sep 2026): https://www.spglobal.com/spdji/en/documents/index-news-and-announcements/iTraxx%20Europe%20Series%2046%20Version%201.pdf
- S&P Global, iTraxx RED codes, September 2026: https://www.spglobal.com/spdji/en/documents/index-news-and-announcements/20260902-itraxx-red-codes-identifiers-september-2026.pdf
- S&P Global, CDS reference data, RED index codes (Crossover S46 2I667KND6): https://www.spglobal.com/spdji/en/documents/additional-material/spgmi-red-cds-index-codes.pdf
- S&P Global, iTraxx Series 45 RED codes (Crossover S45 2I667KNC8, Main S45 2I666VDN2): https://www.spglobal.com/spdji/en/documents/index-news-and-announcements/20260303-itraxx-itraxx-series-45-red-codes.pdf
- ICE contract spec, Markit iTraxx Europe Main (roll-to-maturity rule): https://www.ice.com/products/28687612/Markit-iTraxx-Europe-Main (search snippet only; ICE is parked for access)
- FCA Handbook MAR 11 Annex 2 (fields, VOLO/FULV): https://handbook.fca.org.uk/handbook/mar11/mar11s7
- FCA Handbook MAR 11.5 (volume deferral flag use): https://handbook.fca.org.uk/handbook/mar11/mar11s5
- FCA PS24/14 (UPI or ISIN, flag deletions): https://www.fca.org.uk/publication/policy/ps24-14.pdf
- ANNA DSB, UPI and OTC ISIN best-practice FAQs (series and version in ISIN granularity): https://www.anna-dsb.com/wp-content/uploads/2024/01/UPI-OTC-ISIN-Best-Practice-FAQs-1.pdf
