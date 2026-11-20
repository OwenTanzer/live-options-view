# KOR public CFTC credit API: documentation check (OA-242)

Documentation only, read on 2026-11-20.
- **No** polling, signup, terms acceptance, outreach, implementation or provider
  change. **No tick data was requested.**
- **Read:** the published OpenAPI document, the public ticker web app's own static
  JavaScript (build `f9d03f93b`), KOR's CFTC-filed rulebook, and KOR's public-pages
  guide.
- Every point below is labelled **Official** (KOR's own published or filed
  documents), **Observed** (KOR's shipped public UI code or page), or **Gap**.

Scope:
- This covers only KOR's **public CFTC Part 43 ticker**: `public.cftc.korfinancial.com`
  for the UI and `ticker.cftc.korfinancial.com` for the API.
- KOR's **client reporting APIs** (`reporting.korfinancial.com`, the SDR submission
  interfaces in the KOR Technical Specifications) are a separate, credentialed
  service, and are not evaluated.
- KOR's **SEC SBSDR registration** (order of 2024-11-04) is also separate. No public
  SEC ticker was found. KOR's site links public pages only for CFTC, Canada and
  ASIC. The shared front-end code contains an `SEC_SBR` / `PUBLIC` category, but no
  public SEC host was identified (**Gap**).

## 1. Official schema

Source: `https://ticker.cftc.korfinancial.com/v3/api-docs`, unauthenticated.
OpenAPI 3.1.0, `info.version` "v0", title "OpenAPI definition" (generated).
- sha256 of the copy read: `9ace4813ff6f633b006691ecf5c07fd590f9aa430af0f83b17cdc99ae114a711`
- The older `/v2/api-docs` answers 401.
- KOR's [public-pages guide](https://public.cftc.korfinancial.com/assets/query-data-on-the-public-pages.pdf)
  points to this OpenAPI documentation (Official).

**Endpoints:**
- `GET /ticks`: optional query parameters `cursor` (string), `query` (string) and
  `pageSize` (int32). Returns `SearchResultDtoTickDto` = `{cursor, hasMore, items[TickDto]}`.
- `GET /ticks/{id}`: returns `TickDetailsDto`.

**`TickDto`:**
- `disseminationIdentifier`, `timestamp`, `assetClass`, `actionType`, `eventType`
  and `product` (all strings);
- `notional` and `price`, each `{leg1, leg2}` of `{amount: number, currency: string}`;
- `effectiveDate` and `expirationDate` (dates).

**`TickDetailsDto`:** the same fields plus `tickFields[]` of
`{category, name, value}`. This is where the full Part 43 field set would appear,
but the field names are not enumerated in the spec (**Gap**).

**Numbers:** the UI parses responses as big-number JSON (`storeAsString: true`),
so identifiers and amounts should be kept as strings (Observed).

**Not in the spec (Gap):** field semantics, timestamp meaning (dissemination vs
execution), `product` coding, enumerations for `actionType`/`eventType`, any
`query` grammar, error schema, rate limits and versioning policy.

## 2. Filter, cursor and replay semantics

- **Credit filter (Observed, UI code):** the Credit tab sends
  `query={"p":["assetClass"],"cf":"eq","v":["CR"]}`, a JSON string. Compound filters
  use `{"lf":<AND|OR>,"q":[...]}`.
  - Operators in the UI code: `eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `co`, `bt`,
    `null`, `nnull`, `sw`, `ew`, `in`, `nin`, `all`.
  - Whether the server accepts operators on fields other than `assetClass`, such as
    a timestamp, is **not documented (Gap)**.
- **Cursor (Observed, UI code):** opaque. The UI sends `pageSize: 25`, keeps the
  returned `cursor` and, while `hasMore` is true, uses it for "load more". A filter
  change resets the cursor.
- **Auto-refresh (Observed, UI code):** "Auto Updates" re-queries from the top, with
  no cursor, every **5 seconds** (`REFRESH_INTERVAL` factory `5e3`).
- **Replay (Gap):** no since/after or sequence parameter and no documented sort
  order (KOR's own example is not monotonic). Cursor stability under new arrivals
  and cursor expiry are undocumented. Deterministic replay, or a gap-free resume
  after downtime, is therefore not established.
- **History depth (Official):** rulebook §11.1.2: data stays available "on the KOR
  website for one year after the initial Public Dissemination".
  - **Gap:** whether the API, rather than only the website, exposes the full year.

## 3. Published access rules

- **Rulebook (Official):** KOR CFTC SDR Rulebook v1.1, "KOR Public", effective 2
  April 2025, as filed with the CFTC ([cftc.gov](https://www.cftc.gov/filings/orgrules/rules05082520999.pdf)).
  - "Public Dissemination … means to make freely available and readily accessible to
    the public … in a consistent, usable, and machine-readable electronic format that
    allows the data to be downloaded, saved, and analyzed" (definitions, p.8).
  - "…available on the KOR website for one year after the initial Public
    Dissemination … instructions freely available … Swap Transaction and Pricing Data
    that is Publicly Disseminated shall be made available free of charge" (§11.1.2).
  - KOR disseminates ASATP, subject to Part 43 time delays (§9.2). Block and large
    notional delays apply (§10.5, §11.1.9).
  - An `EROR` received before initial dissemination means the swap is never
    disseminated (p.40).
  - For credit, "the actual underlying asset(s)" are disseminated (§11.1.4).
  - **Gap:** later rulebook amendments may exist; a KOR amendment submission is
    hosted on KOR's CDN, but it was not read.
- **Authentication (Official + Observed):** the guide's `curl` example uses no
  credentials. The OpenAPI document is served without authentication. The ticker
  page showed no terms-acceptance gate at the single prior read (Observed,
  2026-11-20).
- **Website terms (Gap):** KOR's Acceptable Use Policy
  (`korfinancial.com/documents/KOR-Acceptable-Use-Policy-Sep-2022.pdf`) returns
  AccessDenied. Whether it, or other site terms, apply to the public ticker or API,
  and what they say about automated access and retention, is unknown.
- **Rate limits (Gap):** not published.

## 4. Evidence of actual credit publications

- **Official:** KOR SDR is registered for and "accepts data in … credit default
  swaps" (rulebook p.1, §8.1). The CFTC's 2022-03-30 provisional registration covers
  credit.
- **Gap: no first-party evidence of actual credit publications was found.** KOR's
  site, the rulebook, the fee schedule and the CFTC and SEC records describe
  capability, not activity. They publish no credit trade counts, client
  announcements or volume statistics.
- The credit tab exists in the UI, and `CR` is the UI's default tab. Neither shows
  that credit trades are published.
- At the single prior read (2026-11-20) the credit view showed "No data available"
  (Observed). As Chej noted, one empty view does not establish volume either way.

## Recommendation: one minimal next check (needs approval; not done)

A **single, bounded, read-only enumeration** of KOR's public credit ticks, to answer
the one open question: are any credit publications retained in the one-year window?

- **Request:** `GET https://ticker.cftc.korfinancial.com/ticks?query={"p":["assetClass"],"cf":"eq","v":["CR"]}&pageSize=100`.
  Follow `cursor` once until `hasMore` is false, or a hard cap (for example 50
  pages) is reached, at about 1 request per second. No repeat and no auto-refresh.
- **Keep:** each raw response, with sha256, outside the repository.
- **Report:**
  - total credit ticks and date range;
  - counts by `actionType` / `eventType` / `product`;
  - whether `timestamp` order is monotonic, i.e. whether paging stayed stable;
  - one `/ticks/{id}` detail for the `tickFields` names.
- **Why it is the minimal check:** it is the only way to turn "credit filter
  available" into "credit publications exist", and the rulebook says this data is
  free, machine-readable and meant to be "downloaded, saved, and analyzed".
- **Before running it:** Owen or Chej should approve it as a one-time read, given
  that the website terms (Acceptable Use Policy) are unreadable.

**If it is not approved, the exact missing evidence is:**
1. a first-party count of KOR CFTC credit publications, or one dated credit tick;
2. the terms governing the public ticker or API;
3. the `query` grammar beyond `assetClass` and the cursor semantics, which are
   needed for gap-free resume.

The DTCC pilot is unaffected. Tradeweb stays parked.
