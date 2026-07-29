#!/usr/bin/env python3
"""Runtime smoke check for reddit_sentiment_qqq's browser fallback.

Proves that *this exact environment* -- base image plus whatever version of
the `playwright` pip package `requirements.txt` pins -- can actually launch
Chromium and load a page, not merely that `pip install playwright` and
`import playwright` succeeded. Those two can diverge: the Python package is
a driver/CLI, and the browser binaries it drives come from a separate,
version-matched download (baked into the `mcr.microsoft.com/playwright/python`
base image `crassus/Dockerfile` uses). A mismatch between the pinned pip
version and the image tag leaves `playwright` importable but unable to find
a working Chromium -- exactly the failure mode that would otherwise stay
invisible until a live deploy hits the browser fallback for the first time.

Run as a Docker build step (see `crassus/Dockerfile`) so that mismatch fails
the build itself, not a production RedditFetchError days later:

    python scripts/smoke_browser_launch.py
"""

from __future__ import annotations


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
    except ImportError as exc:
        print(f"FAIL: playwright package not importable: {exc}")
        return 1

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto("about:blank")
                page.close()
            finally:
                browser.close()
    except Exception as exc:
        print(f"FAIL: could not launch Chromium in this environment: {exc}")
        return 1

    print("OK: Chromium launched and loaded a page in this environment")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
