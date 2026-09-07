#!/usr/bin/env python3
"""One-shot, read-only tastytrade OAuth and DXLink production probe.

This script never submits, previews, modifies, or cancels an order. It exchanges
the configured refresh token, performs GET-only API calls, subscribes to two QQQ
option symbols plus QQQ market data, confirms that option data arrives, and exits.
"""

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collector


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm-read-only",
        action="store_true",
        help="acknowledge that this makes a real read-only tastytrade/DXLink probe",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="maximum seconds to wait for option data (default: 30)",
    )
    args = parser.parse_args()

    if not args.confirm_read_only:
        parser.error("--confirm-read-only is required")
    if not 5 <= args.timeout <= 120:
        parser.error("--timeout must be between 5 and 120 seconds")

    token_manager = collector.OAuthTokenManager.from_env()
    auth = collector.tasty_auth(token_manager)
    today = datetime.now(collector.ET).date()
    strikes, expiration = collector.load_chain(auth["access_token"], today)
    if not strikes:
        raise RuntimeError("read-only probe found no option strikes")

    middle = strikes[len(strikes) // 2]
    option_symbols = [middle["call_sym"], middle["put_sym"]]
    feed = collector.DXLinkFeed(auth["streamer_url"], auth["streamer_token"])
    feed.set_subscriptions(option_symbols, [collector.TICKER])
    feed.start()

    try:
        if not feed.wait_ready(timeout=min(args.timeout, 20.0)):
            raise RuntimeError("DXLink channel did not become ready")

        deadline = time.monotonic() + args.timeout
        received_options = []
        while time.monotonic() < deadline:
            state = feed.get_state()
            received_options = [symbol for symbol in option_symbols if state.get(symbol)]
            if received_options:
                break
            time.sleep(0.25)

        if not received_options:
            raise RuntimeError("DXLink produced no option data before the timeout")

        health = feed.get_health()
        if not health["authorized"] or not health["channel_open"]:
            raise RuntimeError("DXLink lost authorization before probe completion")

        print(
            "PASS read-only tastytrade OAuth/DXLink probe: "
            f"expiration={expiration} option_symbols_with_data={len(received_options)}"
        )
        return 0
    finally:
        feed.stop()


if __name__ == "__main__":
    raise SystemExit(main())
