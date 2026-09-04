#!/usr/bin/env python3
"""Log a browser into a configured site - and keep the password away from the model.

    python tools\\playwright\\weblogin.py ionos            log in, save the session
    python tools\\playwright\\weblogin.py ionos --check    is the saved session still good?
    python tools\\playwright\\weblogin.py --list           what is configured

THE POINT OF THIS FILE: a desk must never hold a password. It holds a SITE
NAME. This tool reads config\\websites.ini for the url/user/selectors, reads the
password from the root .env (which desks are denied from reading), types it
into the browser itself, and saves the authenticated session to
state\\web\\<site>.json. Everything after that - the desk driving pages, reading
tables, filling forms - runs on a session that needs no secret at all.

2FA: if the site asks for a one-time code, this opens a request the watchdog
posts to Discord (docs\\WEB.md W3), waits for the owner's six digits, types
them, and forgets them. It never retries: an unanswered ask fails closed and
this reports "needs your code" rather than hammering a login until the account
locks.

SCRIPTED LOGIN IS THE SECOND-BEST OPTION and this file says so out loud. A
site behind a login you can reach in your own browser is better driven by the
Claude Chrome extension: you sign in by hand once, the session lives in the
browser profile, nothing is ever scripted and no password exists in this
workspace at all. Use this tool for the sites where that is not practical.
"""
import argparse
import json
import re
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import omnius_config as ocfg  # noqa: E402

STATE = ROOT / "state"
WEB = STATE / "web"                      # <site>.json = saved storage state
TWOFA = STATE / "twofa"                  # the code relay (watchdog posts/answers)
TWOFA_WAIT = 120                         # must match watchdog.TWOFA_WAIT_SECONDS

# Best-effort selectors, tried in order. A site whose form is unusual gets
# explicit *_selector entries in its websites.ini block - guessing is a
# convenience, never a promise.
USER_GUESSES = ("input[type=email]", "input[name*=user i]", "input[name*=email i]",
                "input[id*=user i]", "input[id*=email i]", "input[type=text]")
PASS_GUESSES = ("input[type=password]", "input[name*=pass i]", "input[id*=pass i]")
SUBMIT_GUESSES = ("button[type=submit]", "input[type=submit]",
                  "button:has-text('Log in')", "button:has-text('Sign in')",
                  "button:has-text('Iniciar')", "button:has-text('Entrar')")
OTP_GUESSES = ("input[name*=otp i]", "input[name*=code i]", "input[id*=otp i]",
               "input[id*=code i]", "input[autocomplete='one-time-code']",
               "input[inputmode=numeric]")


def say(msg):
    print(msg, flush=True)


def fail(msg, code=2):
    say(f"[X] {msg}")
    sys.exit(code)


def first_visible(page, configured, guesses, timeout=4000):
    """-> a locator for the first selector that is actually on the page."""
    tries = ([configured] if configured else []) + list(guesses)
    for sel in tries:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=timeout if sel == tries[0] else 800)
            return loc
        except Exception:                                    # noqa: BLE001
            continue
    return None


def ask_for_code(site, user, session):
    """Open a 2FA request and wait for the owner's answer. -> code or None.

    The request is a FILE the watchdog posts and answers; nothing here talks to
    Discord, exactly like the permission relay. Fails closed on silence."""
    TWOFA.mkdir(parents=True, exist_ok=True)
    rid = f"{site}-{uuid.uuid4().hex[:8]}"
    req = TWOFA / f"{rid}.json"
    ans = TWOFA / f"{rid}.code"
    req.write_text(json.dumps({"id": rid, "site": site, "user": user,
                               "session": session, "askedTs": time.time()}),
                   encoding="utf-8")
    say(f"[..] {site} wants a 6-digit code - asked in Discord, waiting up to "
        f"{TWOFA_WAIT // 60}m")
    deadline = time.time() + TWOFA_WAIT
    while time.time() < deadline:
        if ans.is_file():
            code = ans.read_text(encoding="utf-8").strip()
            ans.unlink(missing_ok=True)
            req.unlink(missing_ok=True)
            return code if re.fullmatch(r"\d{6}", code) else None
        if not req.is_file():
            break                                    # swept as expired
        time.sleep(1.0)
    req.unlink(missing_ok=True)
    ans.unlink(missing_ok=True)
    return None


def logged_in(page, success_selector):
    """Did the login take? A configured success_selector is the honest answer;
    without one, 'no password field visible any more' is the best guess."""
    if success_selector:
        try:
            page.locator(success_selector).first.wait_for(state="visible", timeout=8000)
            return True
        except Exception:                                    # noqa: BLE001
            return False
    try:
        page.locator("input[type=password]").first.wait_for(state="visible", timeout=3000)
        return False
    except Exception:                                        # noqa: BLE001
        return True


def run(site_name, check_only=False, headed=False):
    sites = ocfg.websites()
    site = sites.get(site_name.strip().lower())
    if not site:
        fail(f"'{site_name}' is not in config\\websites.ini "
             f"(configured: {', '.join(sorted(sites)) or 'none'})")
    url, user = site["url"], site["user"]
    sel = site.get("selectors") or {}
    env_key = site.get("password_env") or ""

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        fail("playwright is not installed - see tools\\playwright\\README.md")

    WEB.mkdir(parents=True, exist_ok=True)
    store = WEB / f"{site_name.strip().lower()}.json"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        ctx = browser.new_context(storage_state=str(store) if store.is_file() else None)
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        if logged_in(page, sel.get("success_selector")):
            say(f"[OK] {site_name}: already signed in (saved session still valid)")
            ctx.storage_state(path=str(store))
            browser.close()
            return 0
        if check_only:
            say(f"[!] {site_name}: the saved session is NOT valid - run without --check")
            browser.close()
            return 1
        if not env_key:
            browser.close()
            fail(f"{site_name} has no password_env: it is a sign-in-by-hand site. "
                 f"Log in once in your own browser and let the desk drive it with "
                 f"the Chrome extension (docs\\WEB.md).")
        password, _ = ocfg.secret({"password_env": env_key})
        if not password:
            browser.close()
            fail(f"{env_key} is not set in .env - add it there, never here")

        u = first_visible(page, sel.get("user_selector"), USER_GUESSES)
        pw = first_visible(page, sel.get("pass_selector"), PASS_GUESSES, timeout=4000)

        # TWO-STEP LOGINS (Zoho, Google, Microsoft): the first page has only the
        # email and a "Next" button; the password field does not exist yet.
        # Filling what is there and pressing Enter walks to step two, and only
        # then is there a password to type. Without this, weblogin reached the
        # password page with nothing filled and reported "still not signed in"
        # - a confusing way to say "this form has two pages" (2026-09-04, Zoho).
        if u and not pw:
            if user:
                u.fill(user)
            nxt = first_visible(page, sel.get("next_selector"), SUBMIT_GUESSES, timeout=2000)
            if nxt:
                nxt.click()
            else:
                u.press("Enter")
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:                                # noqa: BLE001
                pass
            pw = first_visible(page, sel.get("pass_selector"), PASS_GUESSES, timeout=10000)
            u = None                     # already submitted; do not retype it

        if not pw:
            browser.close()
            fail(f"could not find the login form on {url}. Add user_selector / "
                 f"pass_selector to [{site_name}] in config\\websites.ini")
        if u and user:
            u.fill(user)
        pw.fill(password)                     # the ONLY place the secret is used
        del password
        btn = first_visible(page, sel.get("submit_selector"), SUBMIT_GUESSES, timeout=2000)
        if btn:
            btn.click()
        else:
            pw.press("Enter")
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:                                    # noqa: BLE001
            pass

        otp = first_visible(page, sel.get("otp_selector"), OTP_GUESSES, timeout=6000)
        if otp:
            code = ask_for_code(site_name, user, "orchestrator")
            if not code:
                browser.close()
                say(f"[X] {site_name}: no 2FA code arrived - login stopped, nothing retried")
                return 3
            otp.fill(code)
            del code
            btn2 = first_visible(page, sel.get("submit_selector"), SUBMIT_GUESSES, timeout=2000)
            if btn2:
                btn2.click()
            else:
                otp.press("Enter")
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:                                # noqa: BLE001
                pass

        if not logged_in(page, sel.get("success_selector")):
            # "Still not signed in" alone sends the reader guessing. Say WHERE
            # the browser ended up and what the page is showing - a wrong
            # password, a captcha and a push-approval wall all look identical
            # from the outside otherwise (2026-09-04).
            try:
                where = page.url
                shown = " / ".join(
                    t.strip() for t in page.locator("body").inner_text().splitlines()
                    if t.strip()
                )[:300]
            except Exception:                                # noqa: BLE001
                where, shown = "?", "?"
            say(f"    ended at: {where}")
            say(f"    page says: {shown}")
            browser.close()
            say(f"[X] {site_name}: still not signed in. Add success_selector to its "
                f"block so this can tell, or the site may be refusing scripted logins "
                f"- use the Chrome extension for it.")
            return 4
        ctx.storage_state(path=str(store))
        browser.close()
    say(f"[OK] {site_name}: signed in, session saved to state\\web\\{site_name}.json")
    say("     desks can now drive it with no secret involved")
    return 0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("site", nargs="?", help="a section name from config\\websites.ini")
    ap.add_argument("--check", action="store_true", help="only test the saved session")
    ap.add_argument("--headed", action="store_true", help="show the browser (debugging)")
    ap.add_argument("--list", action="store_true", help="list configured sites")
    a = ap.parse_args(argv)
    if a.list or not a.site:
        rows = ocfg.website_status()
        if not rows:
            say("no sites configured - copy config\\websites.example.ini to websites.ini")
            return 0
        for site, url, user, env_key, state in rows:
            say(f"{site:18} {url:38} {user or '-':26} {env_key or '(browser session)'}: {state}")
        return 0
    return run(a.site, check_only=a.check, headed=a.headed)


if __name__ == "__main__":
    sys.exit(main())
