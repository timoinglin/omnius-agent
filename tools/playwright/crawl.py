#!/usr/bin/env python3
"""Crawl a site with one browser and several tabs, and SAVE rather than print.

    python tools\\playwright\\crawl.py https://example.com
    python tools\\playwright\\crawl.py https://example.com --depth 3 --max-pages 80
    python tools\\playwright\\crawl.py https://example.com --concurrency 6   # your own site

Two decisions shape this tool.

**One browser, many tabs.** Measured 2026-08-09 over six real pages: sequential
6.5s, six tabs on one browser 2.2s, six separate `fetch.py` processes 7.7s -
SLOWER than sequential, because each process pays a fresh Chromium launch
(~1-2s, ~150MB). So parallelism belongs inside one browser, never in a fan-out
of fetch.py.

**It prints a map and writes the text to disk.** Fifty pages of body text piped
into a desk's context would cost more than the crawl saves and drown whatever
the desk was actually doing - the same lesson `tools\\transcribe\\` learned with
2-hour transcripts. You get an index; you Read only the pages you need.

Public pages only. Anything behind a login belongs in the Claude Chrome
extension - see README.md.
"""
import argparse
import asyncio
import json
import re
import sys
import urllib.error
import urllib.request
import urllib.robotparser
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urldefrag, urlparse

ROOT = Path(__file__).resolve().parent.parent.parent
SKIP_EXT = (".pdf", ".zip", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
            ".mp4", ".webm", ".mp3", ".css", ".js", ".woff", ".woff2", ".ttf", ".xml")


def norm(url):
    """Drop the fragment and any trailing slash, so /a, /a/ and /a#x are one page."""
    url, _ = urldefrag(url)
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return None
    path = p.path.rstrip("/") or "/"
    return f"{p.scheme}://{p.netloc}{path}" + (f"?{p.query}" if p.query else "")


def same_site(url, root_host, allow_sub):
    host = urlparse(url).netloc.lower()
    root = root_host.lower()
    return host.endswith("." + root) or host == root if allow_sub else host == root


DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")


def load_robots(robots_url, ua):
    """-> (RobotFileParser or None, human-readable note).

    NOT RobotFileParser.read(). Two things make it the wrong tool here, both
    measured 2026-08-09 against a real WAF-protected site:

    1. It fetches with `Python-urllib/3.x`, which plenty of WAFs answer with
       403 — so you are judged on a request the site refused, not on its rules.
    2. On 401/403 it sets `disallow_all`, i.e. "crawl nothing". That is
       STRICTER THAN THE STANDARD: RFC 9309 says an unavailable robots.txt
       (4xx) means no restrictions. The combination is silent and total - the
       crawler returns one page and looks broken, while appearing to be
       obeying rules the site never stated.

    So: fetch with a real UA, parse the text ourselves, and follow RFC 9309 -
    4xx means unrestricted, 5xx means back off (the site is not saying yes).
    """
    rp = urllib.robotparser.RobotFileParser()
    try:
        req = urllib.request.Request(robots_url, headers={"User-Agent": ua})
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read().decode("utf-8", "replace")
        rp.parse(body.splitlines())
        return rp, f"honoured ({len(body)} bytes, as {ua.split('/')[0]})"
    except urllib.error.HTTPError as e:
        if 500 <= e.code < 600:                 # unreachable != permission
            return None, f"HTTP {e.code} — server error; crawling anyway is your call"
        return None, f"HTTP {e.code} — no robots.txt, so no restrictions (RFC 9309)"
    except Exception as e:                                       # noqa: BLE001
        return None, f"unreadable ({type(e).__name__}) — proceeding without it"


def slug(url, n):
    p = urlparse(url)
    s = re.sub(r"[^a-z0-9]+", "-", (p.path + ("-" + p.query if p.query else "")).lower()).strip("-")
    return f"{n:03d}-{(s or 'index')[:60]}.md"


async def main():
    ap = argparse.ArgumentParser(description="Crawl a site; save pages, print a map.")
    ap.add_argument("url")
    ap.add_argument("--depth", type=int, default=2, help="link hops from the seed (default 2)")
    ap.add_argument("--max-pages", type=int, default=50, help="hard ceiling (default 50)")
    ap.add_argument("--concurrency", type=int, default=3,
                    help="tabs at once. Default 3 ON PURPOSE: more looks like an attack "
                         "to a WAF on someone else's site. Raise it for your own.")
    ap.add_argument("--out", help="output dir (default media\\crawls\\<host>-<date>)")
    ap.add_argument("--timeout", type=int, default=30, help="seconds per page")
    ap.add_argument("--allow-subdomains", action="store_true")
    ap.add_argument("--user-agent", default=DEFAULT_UA,
                    help="UA for the browser AND the robots.txt check, so the rules "
                         "you are judged by match the request you actually send")
    ap.add_argument("--ignore-robots", action="store_true",
                    help="crawl paths robots.txt disallows. Be sure you have the right.")
    args = ap.parse_args()

    if not args.url.startswith(("http://", "https://")):
        args.url = "https://" + args.url
    seed = norm(args.url)
    if not seed:
        print("not an http(s) URL", file=sys.stderr)
        return 2
    host = urlparse(seed).netloc

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("playwright is not installed. Run install.bat, or:\n"
              "  python -m pip install playwright\n"
              "  python -m playwright install chromium", file=sys.stderr)
        return 2

    rp, robots_note = None, "not checked (--ignore-robots)"
    if not args.ignore_robots:
        rp, robots_note = load_robots(f"{urlparse(seed).scheme}://{host}/robots.txt", args.user_agent)

    def allowed(u):
        return True if rp is None else rp.can_fetch(args.user_agent, u)

    out = Path(args.out) if args.out else (
        ROOT / "media" / "crawls" / f"{host}-{datetime.now().strftime('%Y-%m-%d')}")
    out.mkdir(parents=True, exist_ok=True)

    seen, results, skipped = {seed}, [], []
    ms = args.timeout * 1000

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=args.user_agent)
        sem = asyncio.Semaphore(max(1, args.concurrency))

        async def visit(url):
            """-> (title, text, links) for one page. Never raises."""
            async with sem:
                page = await ctx.new_page()
                try:
                    await page.goto(url, timeout=ms, wait_until="domcontentloaded")
                    try:
                        await page.wait_for_load_state("networkidle", timeout=min(ms, 6000))
                    except Exception:                            # noqa: BLE001
                        pass                                     # a polling page never idles
                    title = (await page.title() or "").strip()
                    text = await page.inner_text("body")
                    hrefs = await page.eval_on_selector_all(
                        "a[href]", "els => els.map(e => e.href)")
                    return title, text, hrefs
                except Exception as e:                           # noqa: BLE001
                    return None, f"{type(e).__name__}: {e}", []
                finally:
                    await page.close()

        frontier, depth = [seed], 0
        while frontier and len(results) < args.max_pages and depth <= args.depth:
            batch = frontier[:max(0, args.max_pages - len(results))]
            frontier = []
            got = await asyncio.gather(*(visit(u) for u in batch))
            for url, (title, text, hrefs) in zip(batch, got):
                if title is None:
                    skipped.append((url, text))                  # text holds the error
                    continue
                n = len(results) + 1
                name = slug(url, n)
                (out / name).write_text(
                    f"# {title or url}\n\n<{url}>\n\ndepth {depth} · crawled "
                    f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n\n---\n\n{text}\n",
                    encoding="utf-8")
                results.append({"n": n, "url": url, "title": title,
                                "chars": len(text), "depth": depth, "file": name})
                if depth < args.depth:
                    for h in hrefs:
                        nh = norm(h)
                        if (nh and nh not in seen and same_site(nh, host, args.allow_subdomains)
                                and not nh.lower().endswith(SKIP_EXT) and allowed(nh)):
                            seen.add(nh)
                            frontier.append(nh)
            depth += 1

        await browser.close()

    (out / "index.json").write_text(json.dumps(
        {"seed": seed, "host": host, "pages": results, "failed": skipped,
         "depth": args.depth, "maxPages": args.max_pages}, indent=2), encoding="utf-8")

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    # Always state what robots did. The bug this replaced was SILENT: the crawl
    # returned one page and looked broken, with nothing saying why.
    print(f"\ncrawled {len(results)} page(s) from {host} -> {out}")
    print(f"  robots.txt: {robots_note}\n")
    print(f"  {'#':>3}  {'depth':<5} {'chars':>7}  {'title':<44} file")
    print(f"  {'-'*3}  {'-'*5} {'-'*7}  {'-'*44} {'-'*20}")
    for r in results:
        print(f"  {r['n']:>3}  {r['depth']:<5} {r['chars']:>7}  {(r['title'] or '')[:44]:<44} {r['file']}")
    if skipped:
        print(f"\n  {len(skipped)} failed:")
        for u, e in skipped[:10]:
            print(f"    {u} — {e[:70]}")
    # Say what was NOT covered. A crawl that silently hit its ceiling reads as
    # "that is the whole site", which is how you draw conclusions from half of one.
    if len(results) >= args.max_pages:
        print(f"\n  ⚠ stopped at --max-pages {args.max_pages}; {len(seen) - len(results)} "
              f"known URL(s) not visited. Raise it to go further.")
    print(f"\n  Read the .md files you need — they are NOT printed here on purpose.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
