# playwright — headless browsing

A real Chromium with no screen, no cookies and no sessions. It exists because a
growing share of the web is an empty HTML shell plus JavaScript: `requests` gets
you the empty part, this gets you what a human would read.

```bash
python tools\playwright\fetch.py <url>                 # readable text
python tools\playwright\fetch.py <url> --html          # rendered HTML
python tools\playwright\fetch.py <url> --shot out.png  # full-page image
python tools\playwright\fetch.py <url> --wait "#main"  # wait for a selector
```

`--timeout` (seconds, default 30), `--width` / `--height`. A bare domain gets
`https://` added — Chromium's error for a missing scheme explains nothing.

For anything beyond fetching, import Playwright directly; the whole API is
available. `fetch.py` is the common case made one command, not a wrapper you
have to go through.

## Crawling a site

```bash
python tools\playwright\crawl.py <url>                          # depth 2, 50 pages
python tools\playwright\crawl.py <url> --depth 3 --max-pages 80
python tools\playwright\crawl.py <url> --concurrency 6          # your OWN site
```

Prints a map — number, depth, size, title, filename — and **writes the page text
to `media\crawls\<host>-<date>\`** with an `index.json`. It does not print the
text, deliberately: fifty pages piped into a desk's context would cost more than
the crawl saves and drown whatever the desk was doing. Read the pages you need.

`--out`, `--timeout`, `--allow-subdomains`, `--user-agent`, `--ignore-robots`.

**Do not parallelise by running several `fetch.py`.** Measured 2026-08-09 on six
real pages: sequential **6.5s**, six tabs in one browser **2.2s**, six separate
`fetch.py` processes **7.7s** — *slower than sequential*, because each pays a
fresh Chromium launch (~1–2s, ~150MB). Concurrency belongs inside one browser,
which is what `crawl.py` does.

**Concurrency defaults to 3.** Six tabs at once against a stranger's site looks
like a small attack to a WAF and earns a 429 or a ban. Raise it for your own.

### robots.txt — and the stdlib trap

Honoured by default, and the run always prints what it decided, because the bug
this replaced was silent.

**`RobotFileParser.read()` is not used, and must not be.** It fetches with
`Python-urllib/3.x`, which many WAFs answer with **403** — and on 401/403 it sets
`disallow_all`, i.e. crawl nothing. That is stricter than the standard: RFC 9309
says an unavailable robots.txt (4xx) means *no restrictions*. Found 2026-08-09
against a real site whose robots.txt says `User-agent: * / Allow: /` and
explicitly welcomes AI crawlers — the crawl still returned exactly one page and
looked broken.

So `crawl.py` fetches robots.txt with a real user-agent, parses it itself, and
follows RFC 9309: 4xx → unrestricted, 5xx → back off. `--user-agent` sets the UA
for **both** the browser and the robots check, so you are judged by the rules
that match the request you actually send.

## Which browser tool — this or the Chrome extension?

**This is the rule, decided 2026-08-07. It is about sessions, not difficulty.**

| Use | Because |
|---|---|
| **Playwright** — public pages, docs, scraping, price checks, JS-built pages, screenshots, filling public forms, anything repeatable or scheduled | A clean browser every time. Nothing to log into, nothing to leak, and it runs unattended in a routine at 03:00. |
| **Claude Chrome extension** — anything behind a login: his dashboards, Zoho, the bank, a webmail UI, an admin panel | It drives **his real browser**, already signed in. The session is his, it stays in his browser, and no credential ever reaches this workspace. |

**Never work around the split by putting credentials in a Playwright script.**
A login here would mean a password in a file, in a scheduled job, on disk —
which is exactly what `.env`-only exists to prevent, and the extension already
solves the problem properly. If a task seems to need a logged-in page
headlessly, that is the signal to ask him, not to script a login.

## Install

`install.bat` handles both halves. The pip package is small and always
installed; the Chromium build is ~150MB and is **asked for**, because a fresh
install on a tethered connection should not silently pull a browser.

Missing it later is not fatal — `fetch.py` says exactly what to run:

```bash
python -m playwright install chromium
```

The browser cache lives in `%LOCALAPPDATA%\ms-playwright`, per user and outside
the workspace, so it never travels in the zip. A moved install re-asks once.

## Notes

- **Headless is the default and should stay it.** A visible window under
  `pythonw` is how the flashing-console bug happened.
- Waits for `networkidle` (best-effort, capped at 8s) rather than `load`: the
  JS pages this tool exists for finish building *after* `load` fires. A page
  that polls forever never goes idle, so the timeout is not treated as failure.
- Output is written as UTF-8 explicitly — a page with an em dash must not die
  on a cp1252 console.
