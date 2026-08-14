#!/usr/bin/env python3
"""Fetch a web page the way a browser sees it - headless, no login.

Why this exists rather than `requests`: a growing share of pages are empty
HTML plus JavaScript. requests gets you the empty part. This runs a real
Chromium, lets the page build itself, and gives you what a human would read.

Why this is NOT the tool for everything: it is a FRESH browser with no
cookies, no history and no sessions. Anything behind a login belongs in the
Claude Chrome extension, which drives the real browser you are already signed
into. Do not put credentials in here to work around that - see README.md.

    python tools\\playwright\\fetch.py <url>                 readable text
    python tools\\playwright\\fetch.py <url> --html          rendered HTML
    python tools\\playwright\\fetch.py <url> --shot out.png  full-page image
    python tools\\playwright\\fetch.py <url> --wait "#main"  wait for a selector
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def main():
    ap = argparse.ArgumentParser(description="Render a page headlessly and print what it says.")
    ap.add_argument("url")
    ap.add_argument("--html", action="store_true", help="rendered HTML instead of text")
    ap.add_argument("--shot", metavar="PATH", help="also save a full-page screenshot")
    ap.add_argument("--wait", metavar="SELECTOR", help="wait for this selector before reading")
    ap.add_argument("--timeout", type=int, default=30, help="seconds (default 30)")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=900)
    args = ap.parse_args()

    if not args.url.startswith(("http://", "https://")):
        # A bare domain is the common slip and the error Chromium gives for it
        # ("net::ERR_ABORTED") explains nothing. Fix it rather than report it.
        args.url = "https://" + args.url

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed. Run install.bat, or:\n"
              "  python -m pip install playwright\n"
              "  python -m playwright install chromium", file=sys.stderr)
        return 2

    ms = args.timeout * 1000
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": args.width, "height": args.height})
            page.goto(args.url, timeout=ms, wait_until="domcontentloaded")
            if args.wait:
                page.wait_for_selector(args.wait, timeout=ms)
            else:
                # networkidle, not load: the JS-built pages this tool exists for
                # finish AFTER load fires. Best-effort - a page that polls
                # forever never goes idle, and a timeout here is not a failure.
                try:
                    page.wait_for_load_state("networkidle", timeout=min(ms, 8000))
                except Exception:
                    pass
            if args.shot:
                shot = Path(args.shot)
                shot.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(shot), full_page=True)
                print(f"[shot] {shot}", file=sys.stderr)
            out = page.content() if args.html else page.inner_text("body")
            browser.close()
    except Exception as e:                                       # noqa: BLE001
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1

    # Explicit UTF-8: a page with an em dash must not die on a cp1252 console.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
