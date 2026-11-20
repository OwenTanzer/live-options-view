# Tradeweb UK delayed data: go/no-go note for iTraxx Europe Main/Crossover 5y (OA-242)

Public documentation only, read on 2026-11-20.
- **No** signup, terms acceptance, outreach, polling, code or provider change.
- The delayed **data** was **not** opened or downloaded. Tradeweb's terms are
  accepted "by downloading or otherwise using it", so inspecting the files would
  itself be acceptance.
- Every point below is labelled **Documented** (Tradeweb or FCA text says it),
  **Regulatory** (follows from the rules, not from a Tradeweb layout), or **Gap**.

Sources:
- [UK Market Data Policy and Fee Schedule, published 17 Aug 2026, applies from 23 Aug 2026](https://www.tradeweb.com/4a3842/globalassets/disclosures/08.17.26/current-docs/market-data-policy-and-fee-schedule---tradeweb-europe-limited---uk-apa-and-uk-mtf.pdf) ("Policy")
- [Annex B Fee Schedule v2.0, Aug 2026](https://www.tradeweb.com/4a4d3a/globalassets/disclosures/08.17.26/mifid-ii-transparency-market-data--annex-b--fee-schedule--version-2.0_august-2026.pdf) ("Annex B")
- [UK MiFID II Delayed Data Terms and Conditions, last updated 17 Aug 2026](https://www.apa.tradeweb.com/pages/apa-terms.html) ("Terms")
- [Tradeweb APA page](https://www.tradeweb.com/our-markets/data-analytics/trade-reporting-services-apa)
- [UK/EU MTF liquidity-provider schedule Q2 2026](https://tradeweb.com/4a0ae2/globalassets/disclosures/mtf-rulebook/2026/tradeweb-liquidity-provider-uk-eu-mtf-schedule-q2-2026.pdf)
- [FCA PS24/14](https://www.fca.org.uk/publication/policy/ps24-14.pdf)

## Verdict: NO-GO for now; conditional GO after three decisions

Nothing in the documents rules the source out. But building on it now would mean
assuming permission and coverage, which the brief excludes. Before any
implementation, these must be settled:

1. **Licence position (Owen).** Annex B says UK delayed data is free "for the
   recipient's own use", but "its use in a value-added service, is chargeable". The
   Terms list "aggregating with other data sets or creating historical series" as an
   added-value example, though they tie the fee to cases "where you charge fees,
   whether directly or indirectly". A private tape that merges this with DTCC and
   keeps history could fall on either side.
   - **Gap:** the documents do not settle whether non-fee internal aggregation and
     history count as chargeable value-added use.
   - The documented paid alternative is the **Firm Use** licence: £13,200 a year per
     data set (TREA and TREU = £26,400). It expressly covers non-display, derived and
     value-added use, real-time and intraday data, and display to authorised users on
     the firm's own systems.
2. **Acceptance mechanics (Owen).** The Terms are accepted by downloading or using
   the data. Tradeweb may change them at any time, effective on posting, and users
   "are expected to check this page each time". An automated collector would accept
   them on every run and would need to re-check the Terms on each access.
3. **iTraxx identification and layout (no access without step 2).**
   - Field layout and file cadence of the free files are not publicly documented
     outside the shared location. Opening that location means accepting the Terms.
   - Rows identify instruments by ISIN (Regulatory). Mapping an ISIN to "iTraxx
     Europe Main/Crossover, 5y, on-the-run" needs a separate reference-data source,
     such as ANNA DSB derivative ISIN records. **Gap:** not chosen, terms not read.

## Questions answered

### Which delayed dataset carries iTraxx Europe Main and Crossover 5y?

- **Three UK data sets** (Documented: Policy §2, Annex B §1): UK APA post-trade
  (TREA, off-venue trade reports), UK MTF post-trade (TREU, on-venue trades), and UK
  MTF pre-trade (TREU). The Terms cover "the Tradeweb UK MTF (TREU) and Tradeweb UK
  APA (TREA)".
  - One liquidity-provider schedule writes "TREU (EU MTF)". That contradicts the
    Terms, Annex B and the APA page, all of which say TREU is the UK MTF; the EU MTF
    code there is TWEM. I treat it as a typo, noted as an inconsistency.
- **On-venue:** Tradeweb's UK/EU MTF fee schedule lists "EU CDS Index – UK/EU MTF"
  with **EU Main** and **Xover** (Documented). So on-venue iTraxx trades on the UK
  MTF would be in **TREU post-trade**.
  - **Gap:** the schedule covers UK and EU MTFs together, does not state the tenor,
    and gives no UK-only iTraxx volume.
- **Off-venue:** OTC iTraxx trades by UK investment firms that report through
  Tradeweb's APA would be in **TREA** (Regulatory, under PS24/14: Category 1 OTC
  trades must be published through an APA, subject to deferrals). **Gap:** which
  firms report iTraxx through Tradeweb's APA is not public.
- **Size indicator only:** 2025 content disclosure (Policy §6), credit derivatives:
  - UK APA: 7,095 instruments, €27.66bn average daily turnover;
  - UK MTF: 691 instruments, €2.598bn.
  These are not split into index vs single-name or iTraxx 5y. Most of 2025 was
  before the 1 Dec 2025 Category 1 regime.

### What fields and file cadence does it expose?

- **Fields:** "the free delayed post-trade offering [must] contain the prescribed
  post-trade transparency fields and no other field" (Documented: Annex B §1(b)).
  Licensed deliveries may carry extra fields.
  - Expected (Regulatory, UK MiFIR post-trade field set): trading date and time,
    instrument identification code type and ISIN, price, price notation and
    currency, quantity or notional amount and currency, venue of execution
    (MIC/XOFF), publication date and time, venue of publication, transaction
    identification code, and flags, including deferral flags.
  - **Gap:** Tradeweb's actual column names, encoding and file naming are not
    published outside the shared location.
- **Cadence:**
  - Every record, including delayed data, is released to all channels at the same
    time (Documented: Annex B §5).
  - The website "deliver[s] continuously" (Documented).
  - The free delayed files sit at "a shared location linked from
    apa.tradeweb.com", described as "machine readable form", "no registration and no
    credentials" (Documented: Terms).
  - **Gap:** the update interval and file granularity of the free files are not
    documented. Intraday clip files and end-of-day files are licensed channels, not
    the free offering (Documented: Annex B).
- **Timeliness:** at least 15 minutes after publication (Documented). Category 1
  iTraxx trades above the large-in-scale thresholds are deferred under FCA rules:
  price at end of day, size at the end of the following quarter (PS24/14).

### What do the delayed terms say about automated retrieval, retention and internal display?

| Topic | What the documents say | Status |
|---|---|---|
| Automated retrieval | Data is "machine readable", open, "no registration and no credentials"; nothing permits or forbids automated or programmatic retrieval | **Gap: silent** |
| Acceptance | "which you accept by downloading or otherwise using it"; changes effective on posting; check the page "each time you access or use" | Documented |
| Own use | Free "for the recipient's own use" (Annex B); free "for your use in accordance with MiFID II and MiFIR" (Terms) | Documented |
| Fees on use | Fee "where you charge fees, whether directly or indirectly", for redistribution or added-value services (Terms). Annex B adds that UK use "in a value-added service is chargeable". Added-value examples include trading algorithms, TCA, predictive analytics, and "aggregating with other data sets or creating historical series" | **Ambiguous** for a non-fee private tape |
| Retention / archiving | Not addressed for free data. The post-termination retention right in Annex B §4 applies to licensed data | **Gap: silent** |
| Internal display | Not addressed for free data directly. Display to a firm's own authorised users is named under the paid Firm Use licence. A distributor's terminal display of delayed data to users needs no Tradeweb licence (Annex B) | **Gap** for our own private panel |
| Redistribution | At-large redistribution for a fee needs a £13,200 licence. Machine-readable onward delivery must pass on the Terms | Documented; we would not redistribute |

## Coverage limits (unchanged)

- Only trades published through Tradeweb's UK APA and UK MTF. Bloomberg,
  MarketAxess and other venues are separate publishers, and there is no UK
  derivatives consolidated tape.
- Single-name CDS are outside Category 1 (PS24/14 ¶3.18, ¶3.21).
- CDX is not in the UK Category 1 list.

The DTCC pilot is separate and unaffected.
