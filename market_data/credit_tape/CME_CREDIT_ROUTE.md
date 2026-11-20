# CME SDR public credit route: paper check (OA-242)

Read 2026-11-20. No outreach, signup, data polling, implementation or provider change.
**No data file was downloaded.**

**Disclosure:** to read CME's pages I used automated fetch tools, about eight
requests. These covered the SDR pages, the FTP directory **indexes** (file names,
timestamps and sizes only), the FTP legal notice, and the Data Terms. CME's Data
Terms (below) prohibit using scripts, tools or robots to access the website
without prior written permission. I stopped further CME access once I read that.
Owen and Chej should know this exposure exists for any future CME work.

Labels: **Official** (CME's own text), **Observed** (CME directory index metadata),
**Regulatory** (follows from CFTC Part 43, not from a CME document), **Gap**.

## Verdict

**The documented credit file route is daily, so it is reconciliation-only. It is not
cleared even for that, because of CME's terms.** No machine-readable intraday credit
surface is documented.

## Surfaces

### 1. "Immediate" web publication (display)

- **Official** ([repository page](https://www.cmegroup.com/market-data/repository/data.html)):
  data "is pushed to CMEGroup.com immediately upon receipt by the CME Repository
  Services, unless subject to a regulatory-required delay". It is shown on
  per-asset-class tabs, with execution timestamps in UTC, and is "intended for
  direct public use".
- **Official** ([SDR page](https://www.cmegroup.com/trading/global-repository-services/cme-swap-data-repository.html)):
  "published by asset class in real-time … Access history on all previous publicly
  reported swap transactions."
- **Gap:** CME documents no machine-readable interface, API, schema or endpoint for
  this real-time view. The tab content is not in the page's static HTML. I did not
  inspect the page's scripts, because of the terms below.
- **Official:** CME's Data Terms permit use "only … in the form in which it is
  presented on the Website", for "personal use for non-commercial purposes".

### 2. Dated download files (FTP-style HTTP directory)

- **Official:** the Download button on each tab "will direct you to a FTP directory
  on which the files are stored by date (year, month, day)". Each file is a zip
  containing a CSV.
- **Observed** (index metadata, `https://www.cmegroup.com/ftp/sdr/credit/`):
  - Credit files are named `RT.CDS.YYYYMMDD.csv.zip`.
  - **One file per active day:** November 2026 so far has 02, 04, 05, 11, 12, 16, 18
    and 19.
  - Each was last modified around **18:06–18:07 server time** the same day. The time
    zone is not stated.
  - Each is about 1.2–1.5 KB zipped.
  - Credit years are 2013–2020, then **nothing until 2025-11-13**, then every month
    through 2026-11.
  - Rates files follow the same `RT.IRS.YYYYMMDD` daily pattern.
- **Conclusion: these are distinct surfaces.** The website is described as
  immediate; the files are written **once per day, after the day**. The file route
  is therefore **reconciliation-only**.
- **Gap:** whether the files contain records at all, and how many. Small sizes and
  the skipped days suggest sparse activity; that is an inference, not verified.
- **Gap:** a format-change notice was surfaced by search ("CME SDR Part 43
  Real-Time Data will be provided in an updated format. CME SDR will no longer
  generate files specific to each asset class…"). I could not locate it on CME's
  site, and per-asset `RT.CDS` files still appear in 2026.

## Credit schema

- **Regulatory:** public records follow CFTC Part 43 Appendix A, the swap transaction
  and pricing data, as CME's rulebook defines the term.
- **Official, submission side only:** the CME CFTC-rewrite FAQ says CSV is the only
  supported format and credit submissions use `SDRCDS_*`. That describes reporting
  to CME, **not** the public file.
- **Gap:** no CME document describing the **public** `RT.CDS` file columns was found.
  The current CME SDR rulebook (`cme-sdr-rulebook-c.pdf`) could not be retrieved
  (403 or timeout); the CFTC-hosted copies are 2012 drafts.

## Completeness and cadence guarantees

- **Official:** scheduled maintenance runs from Saturday 12 am to Sunday 5 am Central.
  "No new data will be published", and data submitted then "will be processed and
  disseminated once our systems are brought back up."
- **Not documented (Gap):**
  - a file publication time;
  - whether a day with no activity gets a file; skipped days suggest not;
  - whether late corrections are re-published into earlier files;
  - completeness or immutability guarantees.
  - The 2021–2024 credit gap is unexplained.

## Terms (blocking)

[CME Data Terms of Use](https://www.cmegroup.com/trading/market-data-explanation-disclaimer.html)
(last updated 7 Sep 2023), restated in
[Clearing Advisory 23-364](https://www.cmegroup.com/notices/clearing/2023/12/Chadv23-364.html)
(effective 8 Jan 2024):
- **Permitted:** personal, non-commercial use only, in the form presented.
- **Prohibited without prior written authorisation:**
  - "downloading (other than to view only where a link is provided)";
  - "compiling (including … systematic retrieval to create collections,
    compilations, databases…)";
  - transfer "to … others in your company or organization";
  - use of "scripts, software, spiders, robots … to navigate, access, copy in bulk,
    retrieve, harvest, index … any portion of the Website".
- **FTP legal notice** (`/ftp/sdr/ftp-legal-verbiage.txt`): "By utilizing this page
  and downloading the data you agree to the Terms of Use."
- **The repository page adds:** "Vendors and other firms seeking to use the data for
  commercial or other business benefit delivered to clients beyond their own company
  must contact CME Group."
- **Gap: unresolved tension.** CFTC Part 43 requires SDR public data to be freely
  available and machine-readable so it "allows the data to be downloaded, saved, and
  analyzed" (Regulatory). CME's terms do not carve out SDR public data. Whether they
  bind it is a question for CME, i.e. outreach, which is not authorised here.
- **Gap:** the general website terms (`/files/terms-of-use.pdf`) could not be
  retrieved.

## Missing for a live connector

1. A **documented machine-readable intraday credit surface**, with schema, sequencing
   or cursor, and a correction model. None is documented.
2. **CME's written permission** for automated access, retention and internal display
   of SDR public data. The current terms prohibit all three without it.
3. **The public credit file schema** and its publication time, empty-day and
   correction semantics. These are needed even for reconciliation-only use.
4. **Evidence of credit volume.** Not established; file sizes alone do not show it.

KOR stays a candidate. DTCC remains independent and unaffected.
