# LSEG UK delayed-data portal vs Tradeweb for iTraxx Europe Main/Crossover 5y (OA-242)

Read on 2026-11-20. Search-indexed text and public documents only, through search
engines. LSEG's sites were **not** fetched directly. No download, polling, account,
acceptance, contact or implementation.

Labels: **Official** (LSEG's own text, as indexed), **Indexed third-party**,
**Regulatory**, **Gap**.

## Verdict: not a confirmed alternative

There is a real LSEG UK credit publisher, but coverage and permission are both
unverified, and LSEG's website terms prohibit automated access.

## Publisher and data set

- **Publisher: TRADEcho UK APA (MIC `ECHO`), London Stock Exchange.**
  - **Official:** London Stock Exchange's off-book reporting page says TRADEcho
    "provides UK and EU MiFIR OTC and SI trade reporting … authorised to operate as an
    APA by the FCA".
  - **Official:** the TRADEcho PostTrade APA FIX Specification (v1.1.22.0, May 2026)
    has fields "Recommended for OTC/SI trades for 'Credit Derivatives' instruments,
    when TargetAPA=ECHO". So ECHO accepts UK credit-derivative trade reports.
  - **Indexed third-party:** ISDA's credit-derivatives activity reports (2025–2026)
    list "TRADEcho UK APA ECHO UK" among the APAs and venues whose public reports they
    analyse.
- **Data set:** TRADEcho UK post-trade delayed files on LSEG's Delayed Market Data
  portal (`dmd.lseg.com`).
  - **Official:** the portal page says LSEG "makes pre-trade and post-trade data
    available 15 minutes after initial publication".
  - **Indexed third-party:** the portal covers XLON, TGHL and TGHE "plus post-trade
    files for the TRADEcho UK and EU APAs".
  - **Official:** an LSEG notice (30 Jan 2026) covers the **TRADEcho EU** post-trade
    file. No UK-file specification was found.
- **Not a venue for iTraxx:** XLON and Turquoise are equity venues. Category 1 iTraxx
  trades executed **on venue** are published by the venue, such as Tradeweb or
  Bloomberg MTFs, not by LSEG. LSEG could only carry **OTC** iTraxx trades that UK
  firms choose to report through TRADEcho (Regulatory, PS24/14).

## Coverage of iTraxx Europe Main/Crossover 5y: Gap

- No LSEG disclosure of credit-derivative instruments or turnover for ECHO was
  found. Tradeweb publishes a per-asset-class content disclosure (§6); no indexed
  LSEG equivalent turned up.
- Whether any iTraxx 5y prints appear in ECHO, and in what share versus Tradeweb's
  APA and other APAs, is unverified.

## Machine-readable availability: partly indexed, unverified

- **Indexed:** "post-trade files" exist on the portal.
- **Gap:** the format, fields, cadence and naming of the **UK** TRADEcho file. Only
  the EU file notice was found.
- **Gap:** whether downloads need an account. The portal has a register page
  (`dmd.lseg.com/dmd/register.html`). ESMA guidance expects free delayed data without
  registration, but LSEG's own UK practice is not confirmed.

## Non-fee internal monitoring and retention: unverified, and automation looks prohibited

- **Delayed-data T&Cs** (`dmd.lseg.com/delayed-market-data/terms-and-conditions.htm`):
  only the opening clause is search-indexed, and it cites the EU MiFIR and MiFID II
  basis. Clauses on use, retention or automation are **not indexed (Gap)**.
- **LSEG Website Terms of Use (Official, indexed):**
  - "You agree not to use, or attempt to use, any bot, script, automation software,
    or other automated tool or process to access, interact with, or perform any
    function on this Website";
  - "This restriction applies regardless of whether the automated activity is for
    internal, commercial, or personal use."
  - **Gap:** whether these terms govern the `dmd.lseg.com` subdomain, or whether the
    delayed-data T&Cs override them.
- **Older licensed agreements (Official, indexed):** "Redistribution of Delayed data
  is free of charge" (Turquoise agreements 2021/2022). These govern licensed
  customers, not anonymous delayed-portal users.
- **Regulatory (ESMA Q&A, indexed):** venues and APAs may charge for historic data
  "considered as an added-value service". That bears on retention and history, as
  with Tradeweb.

## Exactly what is missing

1. **Coverage:** evidence that ECHO publishes iTraxx Europe Main/Crossover 5y, and
   its share of UK prints.
2. **Data set:** the UK TRADEcho delayed-file specification (fields, cadence, naming)
   and whether an account is needed.
3. **Permission:**
   - the full delayed-data T&Cs on use, retention and automation;
   - whether LSEG's website ban on automation reaches `dmd.lseg.com`.
   As indexed, automated retrieval is prohibited "regardless of … internal" use.
4. **Comparison to Tradeweb:** both are partial publishers. Neither covers on-venue
   Category 1 prints from other venues, and neither has cleared permission.

Tradeweb stays parked. The DTCC pilot is separate.
