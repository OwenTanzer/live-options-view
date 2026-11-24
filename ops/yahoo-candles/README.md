# Yahoo Finance forward receipts

The existing Nasdaq hourly publisher requests five days of one-minute Yahoo Finance chart data and ten days of daily data per listing. With OPTIONSVIEW_YAHOO_RECEIPTS set on its service, those successful response bytes are retained privately by digest and each fetch gets a separate receipt. A receipt records when this collector obtained the chart. Historical exchange timestamps in that chart do not establish earlier collector availability.

The Linux host keeps receipts under ~/.local/share/optionsview-yahoo-candles/data. The hourly timer and publication lock remain the owners of the requests. The installed launcher loads only the receipt-enabled candles and yahoo_receipts modules from its adjacent release directory and then runs the normal Nasdaq publisher. The drop-in is ~/.config/systemd/user/optionsview-nasdaq-hourly.service.d/yahoo-receipts.conf. Removing this drop-in and reloading the user systemd manager restores the ordinary entry point.

Run the private One Clock reader from the repository root:

    node ops/yahoo-candles/replay.mjs ROOT AAPL 1m 2026-11-23 2026-11-24T07:00:00Z

The reader identifies the provider, interval, evidence file, exchange bar timestamp, and first observed revision that qualifies at the cutoff. A missing OHLC field is an unavailable observation. These local receipt times do not assert public Event Builder publication. Cian’s historical ZIP and its release process are independent.
