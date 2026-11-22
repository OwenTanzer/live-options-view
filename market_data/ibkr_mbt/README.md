# Private Micro Bitcoin quote bridge

Run on the same host as an authenticated Interactive Brokers Trader Workstation (TWS) or IB Gateway session with live CME futures-option data. Install the collector dependencies and the packages in requirements.txt inside an isolated environment. From the repository root run `python -m market_data.ibkr_mbt.bridge`. IBKR_HOST defaults to localhost and IBKR_PORT to 4002 (paper Gateway). IBKR_CLIENT_ID defaults to 73.

Set a private IBKR_MBT_QUOTE_KEY on the bridge and Worker. Set IBKR_MBT_QUOTE_ORIGIN on the Worker to the bridge's secured HTTPS ingress. The HTTP service binds only to loopback on port 8789 (IBKR_HTTP_PORT). The bridge requests market data in read-only mode. It qualifies monthly options against the underlying future's conId, multiplier, expiration and last-trade time. Broker account data and broker order submission are outside this integration. Only a current live book with sufficient displayed size and a fresh side change qualifies for a simulated fill. An official CME final mark is required for paper settlement. Restart the bridge after Gateway reconnects.

Qualified market data and a successful authenticated read on the deployed site remain necessary to verify live operation.
