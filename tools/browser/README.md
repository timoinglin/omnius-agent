# tools\browser — open the right Chrome, on the right profile

One script. It starts *your* Chrome so a desk can drive a page through the
Claude Chrome extension, and it does it without asking anyone to click anything.

```
python tools\browser\open_chrome.py --list       what profiles exist, and which is configured
python tools\browser\open_chrome.py              launch the configured profile
python tools\browser\open_chrome.py --url <url>  ... and open a page
python tools\browser\open_chrome.py --status     is Chrome running?
```

## Why it exists

A desk was asked to read a site while Chrome was closed. It ran `chrome`, Chrome
showed its **profile picker**, and the run only worked because the owner
happened to be sitting at the machine to click "Tu Chrome". From Discord on a
phone that click does not exist — the same dead end `[browser] device_id` had
already fixed one level up ("which browser"), reappearing as "which profile".

So the profile is a setting, decided once:

```ini
; config\omnius.ini
[browser]
profile_directory = Default
```

The value is the **directory** name under Chrome's User Data (`Default`,
`Profile 2`, …), not the display name — `--list` prints both, reading Chrome's
own `Local State`, so the list is exactly what the picker would have shown.

It also waits for Chrome to come up before returning: the extension's socket
takes a few seconds, and a desk that calls `list_connected_browsers` immediately
sees an empty list and wrongly concludes there is no browser.

## What it does not do

It does not log in to anything. It starts a browser and stops. Sites whose
session lives in your profile just work; sites that need a scripted login belong
to `tools\playwright\weblogin.py`, and sites behind a corporate identity
provider belong to neither — see [docs\WEB.md](../../docs/WEB.md).
