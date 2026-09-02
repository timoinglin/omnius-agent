#!/usr/bin/env python3
"""Discord REST helper library + CLI for Omnius (stdlib only, Python 3.10+).

Library: imported by watchdog.py. CLI: the admin surface for sessions -
    python tools\\discord\\api.py channels
    python tools\\discord\\api.py ensure
    python tools\\discord\\api.py send --channel orchestrator --text "hello"
    python tools\\discord\\api.py history --channel orchestrator --limit 10
    python tools\\discord\\api.py create-channel --name app --category "RECIPE-APP"
    python tools\\discord\\api.py topic --channel app --text "path | machine | started"
    python tools\\discord\\api.py react --channel app --message <id> --emoji eyes
    python tools\\discord\\api.py pin --channel app --message <id>

`--channel` takes a channel id, a DESK (`orchestrator`, `recipe-app.app`,
`tool.email`), or the channel's name. The desk form is the one that survives:
he may rename #omnius to #maikel in the Discord app whenever he likes, and
routing - this CLI included - follows the pinned channel id, not the name.

Reads root .env itself (DISCORD_BOT_TOKEN, DISCORD_GUILD_ID, ...) - callers
never see the token. Design: docs/ARCHITECTURE.md par. 3.4, docs/DISCORD.md.
"""
import argparse
import base64
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
API_BASE = "https://discord.com/api/v10"
CHANNEL_TEXT, CHANNEL_CATEGORY = 0, 4
MSG_LIMIT = 1990  # Discord cap is 2000; leave headroom for fence repairs
RETRY_AFTER_CAP = 60.0  # refuse to block the single-threaded watchdog longer than this


SNOWFLAKE_RE = re.compile(r"\d{17,20}")  # Discord ids: 17-20 digits, nothing else


def read_env_text(p):
    """Decode a .env written by any Windows tool. PowerShell 5.1's `>` and Out-File
    default to UTF-16, and Notepad may add a UTF-8 BOM - blind utf-8 turns either
    into mojibake and the keys silently vanish."""
    raw = p.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def load_env():
    """Parse root .env. Tolerates what people actually write: quoted values,
    inline comments, `export KEY=`, mixed-case keys, CRLF/LF/CR. Keys are
    upper-cased so lookups never miss on case alone.

    The implementation moved to `tools\\omnius_config.py` on 2026-08-05 so that
    config\\ could resolve secret indirection without importing the Discord
    layer. This delegates to it - one parser, not two that drift apart the
    first time someone fixes an encoding bug in only one of them. The body
    below stays as a fallback: if that module is ever missing or broken, the
    bus must still come up, because nothing can be fixed remotely once Discord
    is down."""
    try:
        sys.path.insert(0, str(ROOT / "tools"))
        import omnius_config
        return omnius_config.load_env(ROOT)
    except Exception:
        pass
    env = {}
    p = ROOT / ".env"
    if not p.exists():
        return env
    # Split on ASCII line breaks ONLY - str.splitlines() also breaks on U+2028
    # and U+0085, which can appear inside a pasted value and truncate a token.
    for line in re.split(r"\r\n|\r|\n", read_env_text(p)):
        if line.lstrip().startswith("#"):
            continue
        m = re.match(r"\s*(?:export\s+)?([A-Za-z0-9_]+)\s*=\s*(.*?)\s*$", line)
        if not m:
            continue
        v = m.group(2)
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]                      # DISCORD_GUILD_ID="123..." -> 123...
        else:
            v = v.split(" #", 1)[0].rstrip()  # trailing inline comment
        env[m.group(1).upper()] = v
    return env


ENV = load_env()
TOKEN = ENV.get("DISCORD_BOT_TOKEN", "")
GUILD = ENV.get("DISCORD_GUILD_ID", "")
OWNER = ENV.get("DISCORD_OWNER_ID", "")
MACHINE = ENV.get("MACHINE_NAME") or os.environ.get("COMPUTERNAME", "unknown")


class ApiError(Exception):
    pass


def intent_status(timeout=20.0):
    """-> (True | False | None, message) for the Message Content Intent.

    THE setting whose failure is silent and total. Without it Discord strips
    text and attachments from every message before anyone here sees them, so
    envelopes arrive with text "" and no files - a fleet that is online,
    reachable, and deaf. Nothing in the REST API reports it: a token check and
    a guild check both pass happily. Only a gateway IDENTIFY finds out, which
    is why this costs one short websocket connect.

    Found the hard way on 2026-08-15: a freshly set-up instance answered its
    owner's first two messages with "it arrived empty", and the desk had to
    diagnose it from the watchdog log. Setup validated the token and the
    server, then sent them off to a bot that could not read.
    """
    if not TOKEN:
        return None, "no token to check with"
    try:
        import gateway as gw
    except Exception as e:                                   # noqa: BLE001
        return None, f"could not load the gateway client ({type(e).__name__})"
    g = gw.Gateway(TOKEN, log=lambda *_a, **_k: None)
    try:
        g.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if g.connected:
                return True, "Message Content Intent is on"
            if g.fatal:
                return False, g.fatal
            time.sleep(0.2)
        return None, f"no answer from the gateway within {int(timeout)}s"
    except Exception as e:                                   # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"
    finally:
        g.stop()


def config_problems():
    """-> list[str] of human-readable config faults. Empty list = usable.

    Presence is not validity: a wrong-but-non-empty id passes every non-empty
    check, then fails deep inside the API where the error is unrecognisable."""
    bad = []
    if not TOKEN:
        bad.append("DISCORD_BOT_TOKEN is empty")
    if not GUILD:
        bad.append("DISCORD_GUILD_ID is empty")
    elif not SNOWFLAKE_RE.fullmatch(GUILD):
        bad.append(f"DISCORD_GUILD_ID is not a Discord id (17-20 digits), got {len(GUILD)} char(s)")
    if not OWNER:
        bad.append("DISCORD_OWNER_ID is empty")
    elif not SNOWFLAKE_RE.fullmatch(OWNER):
        bad.append(f"DISCORD_OWNER_ID is not a Discord id (17-20 digits), got {len(OWNER)} char(s)")
    return bad


def require_config():
    """Gate for REST calls: needs a token and a usable guild id. The owner id
    matters only to the watchdog, which checks config_problems() itself."""
    bad = [b for b in config_problems() if not b.startswith("DISCORD_OWNER_ID")]
    if bad:
        raise ApiError("Discord not configured - " + "; ".join(bad) + " (guided: run install.bat)")


def api(method, path, body=None, params=None, files=None):
    """One REST call with 429/5xx retry. files = list of local paths (multipart upload)."""
    url = API_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for attempt in range(5):
        headers = {"Authorization": f"Bot {TOKEN}", "User-Agent": "Omnius (watchdog, 1.0)"}
        data = None
        if files:
            boundary = f"----OmniusBoundary{int(time.time() * 1000)}"
            parts = [(f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
                      f"Content-Type: application/json\r\n\r\n{json.dumps(body or {})}\r\n").encode()]
            for i, fp in enumerate(files):
                fp = Path(fp)
                ctype = mimetypes.guess_type(fp.name)[0] or "application/octet-stream"
                parts.append((f"--{boundary}\r\nContent-Disposition: form-data; "
                              f"name=\"files[{i}]\"; filename=\"{fp.name}\"\r\n"
                              f"Content-Type: {ctype}\r\n\r\n").encode() + fp.read_bytes() + b"\r\n")
            parts.append(f"--{boundary}--\r\n".encode())
            data = b"".join(parts)
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    wait = float(json.loads(e.read()).get("retry_after", 2))
                except Exception:
                    wait = 2.0
                # Cap it. Discord can hand back retry_after in the hours for a
                # hard rate limit (avatar changes are one), and the watchdog is
                # single-threaded - an uncapped sleep here stops the whole bus.
                if wait > RETRY_AFTER_CAP:
                    raise ApiError(f"rate limited for {wait:.0f}s on {method} {path} "
                                   f"- exceeds the {RETRY_AFTER_CAP:.0f}s cap, not waiting")
                time.sleep(wait + 0.2)
                continue
            if e.code in (500, 502, 503, 504) and attempt < 4:
                time.sleep(1 + attempt)
                continue
            try:
                detail = e.read().decode(errors="replace")[:300]
            except Exception:
                detail = ""
            raise ApiError(f"{method} {path} -> HTTP {e.code} {detail}")
        except urllib.error.URLError as e:
            if attempt < 4:
                time.sleep(2 + attempt)
                continue
            raise ApiError(f"{method} {path} -> network error: {e.reason}")
    raise ApiError(f"{method} {path} -> gave up after retries")


# --- redaction & chunking -----------------------------------------------------

TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,}")

# Until 2026-08-01 TOKEN_RE was the WHOLE filter, so it caught a Discord bot
# token and nothing else. That was found by an escalation test: the permission
# relay posts the command line it is asking about straight into a channel, so
# `curl -H "Authorization: Bot ..."` published the credential to Discord. Same
# path carries session output. CLAUDE.md par.5 says secrets never reach Discord,
# so the filter has to know more shapes than the one we happen to own.
SECRET_RES = [
    TOKEN_RE,
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),               # GitHub PAT / OAuth
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-(?:proj-|ant-)?[A-Za-z0-9_-]{16,}"),     # OpenAI / Anthropic
    re.compile(r"AIza[A-Za-z0-9_-]{30,}"),                   # Google API
    re.compile(r"AKIA[0-9A-Z]{16}"),                         # AWS access key id
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),             # Slack
    re.compile(r"(?i)\b(?:bearer|bot|token)\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]
# key=value / key: value, where the KEY says it is sensitive. Deliberately not
# "any long random string": that would redact commit hashes and file paths, and
# a filter that mangles ordinary output gets turned off.
ASSIGN_RE = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|CREDENTIAL|WEBHOOK|PRIVATE_KEY)[A-Z0-9_]*)"
    r"(\s*[=:]\s*)(['\"]?)([^\s'\"]{4,})")
# Anything under 8 chars, or purely numeric, is not treated as a secret: PORT
# and similar would otherwise blank out every matching number in the text.
_ENV_MIN = 8


def _env_secrets():
    """Literal values from the root .env worth never echoing.

    The strongest filter available: whatever the shape, if it is OUR secret we
    can match it exactly. Only keys that name themselves as sensitive, so
    PORT=8000 does not turn every "8000" in a message into [redacted].
    """
    out = []
    for k, v in (ENV or {}).items():
        if not v or len(v) < _ENV_MIN or v.isdigit():
            continue
        if re.search(r"(?i)TOKEN|SECRET|PASSWORD|PASSWD|KEY|CREDENTIAL|WEBHOOK", k):
            out.append(v)
    return sorted(out, key=len, reverse=True)      # longest first: no partial masking


def redact(text):
    """Secret-shaped strings never reach a channel (ARCHITECTURE par. 7).

    Fails toward over-redacting: a masked value costs one round trip to ask
    again, an unmasked one is in Discord's history for good.
    """
    if not text:
        return text
    text = str(text)
    if TOKEN:
        text = text.replace(TOKEN, "[redacted]")
    for value in _env_secrets():
        text = text.replace(value, "[redacted]")
    for rx in SECRET_RES:
        text = rx.sub("[redacted]", text)
    return ASSIGN_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}[redacted]", text)


def chunk_text(text, limit=MSG_LIMIT):
    """Split into <=limit chunks, preferring newlines, keeping ``` fences valid."""
    chunks, fence_open = [], False
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            piece, remaining = remaining, ""
        else:
            cut = remaining.rfind("\n", 1, limit)
            if cut < limit // 2:
                cut = limit
            piece, remaining = remaining[:cut], remaining[cut:].lstrip("\n")
        if fence_open:
            piece = "```\n" + piece
        fence_open = (piece.count("```") % 2) == 1
        if fence_open:
            piece += "\n```"
        chunks.append(piece)
    return chunks or [""]


# --- guild structure ----------------------------------------------------------

def guild_channels():
    require_config()
    return api("GET", f"/guilds/{GUILD}/channels")


def agent_slug():
    """The owner's name for this agent, as a channel name (config\\omnius.ini
    `[omnius] name`). Lazy import + never raises: a config file may not stop
    the fleet from stamping its structure."""
    try:
        sys.path.insert(0, str(ROOT / "tools"))
        import omnius_config as ocfg
        return ocfg.agent_slug()
    except Exception:                             # noqa: BLE001
        return "omnius"


def resolve_spec_name(ch_spec):
    """The channel name a schema spec asks for, once the owner has had a say.

    `"namedAfter": "agent"` means "call this whatever he calls the agent"
    (config\\omnius.ini `[omnius] name`) - the "name" beside it is the default.

    NOT a {placeholder} in the name itself, which was the obvious design and
    the wrong one: schema.json is read by whatever watchdog is CURRENTLY in
    memory, and an instance that has pulled the new file but not reloaded yet
    would stamp the placeholder verbatim. On 2026-08-24 that created eleven
    #agent channels on a live server, one a minute, before anyone noticed."""
    if isinstance(ch_spec, str):                  # a plain name, nothing to resolve
        return ch_spec
    if (ch_spec or {}).get("namedAfter") == "agent":
        return agent_slug()
    return (ch_spec or {}).get("name", "")


def find_channel(channels, name, ch_type=CHANNEL_TEXT, parent_id=None):
    for c in channels:
        if c["type"] == ch_type and c["name"] == name:
            if parent_id is None or c.get("parent_id") == parent_id:
                return c
    return None


def resolve_channel(name_or_id, category=None):
    """Channel by id, by DESK, or by name (optionally within a category name).

    The desk lookup is what keeps this CLI working after a rename: `--channel
    omnius` must not start failing the day he calls the channel #maikel, and
    `--channel recipe-app.app` should reach that desk whatever its channel is
    called today. Pins first, the literal name second (DISCORD.md par. 2).
    """
    if re.fullmatch(r"\d{15,22}", str(name_or_id)):
        return {"id": str(name_or_id)}
    chans = guild_channels()
    want = str(name_or_id).lstrip("#")
    if not category:
        pins = channel_pins()
        # A desk id ("orchestrator", "recipe-app.app", "tool.email"), or the
        # name a schema channel was created with ("alerts", and "omnius" for a
        # door now called something else - agent_slug() resolves that name to
        # the same pin key).
        keys = [want] + [k for k in pins if k.endswith("#" + want)]
        if want == agent_slug():
            keys.insert(0, "orchestrator")
        for k in keys:
            cid = str((pins.get(k) or {}).get("id") or "")
            ch = next((c for c in chans if str(c["id"]) == cid), None) if cid else None
            if ch:
                return ch
    parent_id = None
    if category:
        cat = find_channel(chans, category, CHANNEL_CATEGORY)
        if not cat:
            raise ApiError(f"category not found: {category}")
        parent_id = cat["id"]
    ch = find_channel(chans, str(name_or_id).lstrip("#"), CHANNEL_TEXT, parent_id)
    if not ch:
        raise ApiError(f"channel not found: {name_or_id}")
    return ch


def create_category(name):
    return api("POST", f"/guilds/{GUILD}/channels", {"name": name, "type": CHANNEL_CATEGORY})


def create_text_channel(name, parent_id=None, topic=""):
    body = {"name": name, "type": CHANNEL_TEXT, "topic": topic[:1024]}
    if parent_id:
        body["parent_id"] = parent_id
    return api("POST", f"/guilds/{GUILD}/channels", body)


def set_topic(channel_id, topic):
    return api("PATCH", f"/channels/{channel_id}", {"topic": topic[:1024]})


def delete_channel(channel_id):
    return api("DELETE", f"/channels/{channel_id}")


def rename_channel(channel_id, name):
    return api("PATCH", f"/channels/{channel_id}", {"name": name})


# --- roles & per-channel permissions ------------------------------------------
#
# Added 2026-08-12, for the first person other than the owner to be let into one
# channel (config\guests.ini). Discord permissions are ADDITIVE: a restrictive
# role grants nothing extra but takes nothing away either, so confining someone
# needs BOTH a closed baseline (@everyone without VIEW_CHANNEL server-wide) and
# an explicit per-channel allow. Granting only the second half looks like a
# boundary and is not one.

PERM_ADD_REACTIONS = 1 << 6
PERM_VIEW_CHANNEL = 1 << 10
PERM_SEND_MESSAGES = 1 << 11
PERM_EMBED_LINKS = 1 << 14
PERM_ATTACH_FILES = 1 << 15
PERM_READ_HISTORY = 1 << 16
# What a guest needs to hold a conversation and nothing more: talk, attach
# images and audio, read what was said before, react. No threads, no embeds of
# other people's messages, no mention-everyone, no slash commands.
PERM_GUEST_IN_CHANNEL = (PERM_VIEW_CHANNEL | PERM_SEND_MESSAGES | PERM_EMBED_LINKS
                         | PERM_ATTACH_FILES | PERM_READ_HISTORY | PERM_ADD_REACTIONS)

OVERWRITE_ROLE = 0
OVERWRITE_MEMBER = 1


def guild_roles():
    return api("GET", f"/guilds/{GUILD}/roles")


def find_role(name):
    """-> the role dict with this exact name (case-insensitive), or None."""
    want = str(name).strip().lower()
    return next((r for r in guild_roles() if str(r.get("name", "")).lower() == want), None)


def ensure_role(name, permissions=0):
    """Create the role if it is missing. -> the role dict. Idempotent.

    `permissions` is the SERVER-WIDE grant and defaults to none: a guest role is
    a label to hang channel overwrites on, not a source of authority.
    """
    existing = find_role(name)
    if existing:
        return existing
    return api("POST", f"/guilds/{GUILD}/roles",
               {"name": name, "permissions": str(int(permissions)),
                "mentionable": False, "hoist": False})


def set_channel_overwrite(channel_id, target_id, allow=0, deny=0,
                          kind=OVERWRITE_ROLE):
    """Replace one role's/member's overwrite on one channel. Idempotent by nature
    (PUT is the whole overwrite, so re-running sets the same state)."""
    return api("PUT", f"/channels/{channel_id}/permissions/{target_id}",
               {"allow": str(int(allow)), "deny": str(int(deny)), "type": int(kind)})


def clear_channel_overwrite(channel_id, target_id):
    return api("DELETE", f"/channels/{channel_id}/permissions/{target_id}")


def everyone_role():
    """The @everyone role. Its id IS the guild id - that is a Discord invariant,
    not a coincidence worth looking up."""
    return next((r for r in guild_roles() if str(r.get("id")) == str(GUILD)), None)


def set_baseline_view(allowed):
    """Turn VIEW_CHANNEL on/off for @everyone SERVER-WIDE. -> the updated role.

    The one switch that decides whether every other permission in this file
    means anything. Kept as its own named function because it is the only
    wide-blast call here: it changes what EVERY member can see in EVERY channel,
    and it is the difference between "she can reach one channel" and "she can
    read the whole fleet, including #daybook". The guild owner is unaffected
    either way - Discord exempts the owner from permission checks - but any
    other human member who relied on the open baseline goes dark, so this is a
    decision to put to him, not one to infer.
    """
    role = everyone_role()
    if role is None:
        raise ApiError("@everyone role not found")
    perms = int(role.get("permissions") or 0)
    new = (perms | PERM_VIEW_CHANNEL) if allowed else (perms & ~PERM_VIEW_CHANNEL)
    if new == perms:
        return role
    return api("PATCH", f"/guilds/{GUILD}/roles/{role['id']}",
               {"permissions": str(new)})


def load_schema():
    return json.loads((ROOT / "tools" / "discord" / "schema.json").read_text(encoding="utf-8"))


def _slug(text):
    """-> a legal Discord channel name: lowercase, [a-z0-9-] only."""
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9-]+", "-", str(text).lower())).strip("-")


def config_channels(spec, log=print):
    """-> [{name, topic}] for a category whose channels come from config.

    `{"config": "email", "group": "account", "prefix": "email-"}` means: one
    channel per `[account.<label>]` in `config\\email.ini`. Adding an account is
    then the ONLY step - the owner never edits schema.json to get a channel
    (his ask, 2026-08-06: "for every added account create a channel").

    Never raises: a missing or broken config yields no channels, because a
    config file must not be able to stop the watchdog from starting.
    """
    if not isinstance(spec, dict):
        return []
    try:
        sys.path.insert(0, str(ROOT / "tools"))
        import omnius_config as ocfg
        labels = ocfg.group(ocfg.load(spec.get("config", "")), spec.get("group", ""))
    except Exception as e:                        # noqa: BLE001 - see docstring
        log(f"[i] config channels for {spec.get('config')}: {type(e).__name__}: {e}")
        return []
    # NAME THE CHANNEL AFTER THE ADDRESS, not the config label. His question,
    # 2026-08-06: "what if i add a second gmail, or three?" - #email-gmail is
    # then meaningless or a collision. `someone@example.com` -> #someone-example
    # ('@' is not a legal Discord channel character). The TLD is dropped because
    # it is noise, and added back ONLY if two accounts would otherwise collide.
    key = spec.get("nameFrom") or ""
    short, long = {}, {}
    for label, body in sorted(labels.items()):
        addr = str((body or {}).get(key) or "")
        local, at, domain = addr.partition("@")
        if at and local and domain:
            parts = [p for p in domain.split(".") if p]
            short[label] = _slug(f"{local}-{parts[0]}")
            long[label] = _slug(f"{local}-{'-'.join(parts)}")
        else:
            short[label] = long[label] = _slug(label)   # no address: label it is
    clash = {n for n in short.values() if list(short.values()).count(n) > 1}

    out = []
    for label, body in sorted(labels.items()):
        name = long[label] if short[label] in clash else short[label]
        if not name:
            continue
        who = str((body or {}).get(key) or "")
        out.append({"name": f"{spec.get('prefix', '')}{name}",
                    "topic": f"{who} — mail in, mail out".strip(" —")})
    return out


### --- channel pins: the desk a channel belongs to, keyed by ID ---------------
#
# Discord routing used to be by channel NAME inside a category, which made a
# rename in the Discord app a silent amputation: #web renamed to #frontend
# looked for a `frontend` component, found none, and the desk went deaf - while
# the next structure stamp helpfully recreated an empty #web beside it.
#
# A name is a label a person changes; an id is what the thing IS. So the first
# time a channel is created or recognised, the desk behind it is PINNED to its
# id here. After that the name is decoration: call it #maikel if you like.
#
# Machine-local (state\, gitignored) because ids belong to one guild, and
# rebuilt from names automatically on any instance that has none yet.

PINS = ROOT / "state" / "watchdog" / "channels.json"


def _bust_pins():
    """Forget the cache after we write - a same-second write can land inside
    the same mtime tick, and a stale pin routes mail to the wrong desk."""
    global _pins_cache
    _pins_cache = (None, {})


_pins_cache = (None, {})    # (mtime_ns, pins) - this is read per envelope


def channel_pins():
    """-> {key: {"id": str, "session": str|None}}. Never raises."""
    global _pins_cache
    try:
        stamp = PINS.stat().st_mtime_ns
    except OSError:
        return {}
    if _pins_cache[0] == stamp:
        return _pins_cache[1]
    try:
        d = json.loads(PINS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    d = d if isinstance(d, dict) else {}
    _pins_cache = (stamp, d)
    return d


def pin_channel(key, channel_id, session=None):
    """Remember which desk this channel id serves. Idempotent, best effort."""
    if not key or not channel_id:
        return
    pins = channel_pins()
    now = {"id": str(channel_id), "session": session}
    if pins.get(key) == now:
        return
    pins[key] = now
    try:
        PINS.parent.mkdir(parents=True, exist_ok=True)
        tmp = PINS.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(pins, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(PINS)
        _bust_pins()
    except OSError:
        pass


def unpin_missing(chans):
    """Drop pins whose channel no longer exists - a deleted channel is not a
    rename, and a stale pin would stop the stamper from recreating it."""
    live = {str(c["id"]) for c in chans}
    pins = channel_pins()
    keep = {k: v for k, v in pins.items() if str(v.get("id")) in live}
    if len(keep) != len(pins):
        try:
            PINS.write_text(json.dumps(keep, indent=2, ensure_ascii=False), encoding="utf-8")
            _bust_pins()
        except OSError:
            pass
    return keep


def pinned_channel(key, chans):
    """-> the live channel dict this key is pinned to, or None."""
    pin = channel_pins().get(key) or {}
    if not pin.get("id"):
        return None
    return next((c for c in chans if str(c["id"]) == str(pin["id"])), None)


def spec_key(cat_name, ch_spec):
    """The identity of a channel spec, independent of what it is called now.

    The session where there is one (that IS the identity); otherwise the name
    it was first created with, scoped by category.
    """
    return ch_spec.get("session") or f"{cat_name}#{ch_spec['name']}"


def ensure_structure(log=print):
    """Stamp schema.json idempotently: find by PIN, then by name, else create.

    The pin comes first so a channel the owner renamed in Discord is recognised
    as the one it already is, instead of being recreated under its old name
    beside itself (DISCORD.md par. 2)."""
    require_config()
    schema = load_schema()
    chans = guild_channels()
    unpin_missing(chans)   # a deleted channel is not a rename
    started = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    for cat_spec in schema["initial"]["categories"]:
        cat = find_channel(chans, cat_spec["name"], CHANNEL_CATEGORY)
        if not cat:
            cat = create_category(cat_spec["name"])
            chans.append(cat)
            log(f"created category: {cat_spec['name']}")
        # Static channels first, then any the config asks for. Both go through
        # the same find-or-create, so this stays idempotent either way.
        specs = list(cat_spec["channels"])
        specs += config_channels(cat_spec.get("channelsFrom"), log=log)
        for ch_spec in specs:
            # Pin first: a channel the owner renamed is still the same
            # channel, and recreating the old name beside it is the
            # unhelpful half of find-or-create.
            key = spec_key(cat_spec["name"], ch_spec)
            want = resolve_spec_name(ch_spec)
            ch = pinned_channel(key, chans) or find_channel(
                chans, want, CHANNEL_TEXT, cat["id"])
            if not ch and "{" not in want and "}" not in want:
                # The brace guard is the scar from the eleven #agent channels
                # (see resolve_spec_name): an unresolved placeholder must
                # never become a real channel, whatever put it there.
                topic = (ch_spec.get("topic", "")
                         .replace("{path}", ".").replace("{machine}", MACHINE)
                         .replace("{started}", started))
                ch = create_text_channel(want, cat["id"], topic)
                chans.append(ch)
                log(f"created channel: #{want}")
            if ch:
                pin_channel(key, ch["id"], ch_spec.get("session"))
    return chans


NOT_COMPONENTS = {"memory", "docs", "media", "state", "node_modules", "__pycache__"}


def project_components(project):
    """Component folders of a project = its session-bearing subdirs."""
    pdir = ROOT / "projects" / project
    if not pdir.is_dir():
        raise ApiError(f"no such project folder: {pdir}")
    return sorted(d.name for d in pdir.iterdir()
                  if d.is_dir() and not d.name.startswith(".")
                  and d.name not in NOT_COMPONENTS)


def ensure_project(project, log=print):
    """Stamp a project's Discord category from schema projectTemplate (idempotent):
    `<prefix><project>` category + #general + one channel per component folder."""
    require_config()
    schema = load_schema()
    tmpl = schema["projectTemplate"]
    cat_name = tmpl["category"].replace("{projectPrefix}", schema["prefixes"]["project"]).replace("{project}", project)
    comps = project_components(project)
    started = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    chans = guild_channels()
    cat = find_channel(chans, cat_name, CHANNEL_CATEGORY)
    if not cat:
        cat = create_category(cat_name)
        chans.append(cat)
        log(f"created category: {cat_name}")
    specs = []
    for ch_spec in tmpl["channels"]:
        if "{component}" in ch_spec["name"]:
            for comp in comps:
                specs.append({k: v.replace("{component}", comp).replace("{project}", project)
                              for k, v in ch_spec.items()})
        else:
            specs.append(ch_spec)
    for ch_spec in specs:
        key = spec_key(cat_name, ch_spec) if ch_spec.get("session") else (
            f"{project}.{ch_spec['name']}" if ch_spec["name"] != "general"
            else f"{cat_name}#general")
        ch = pinned_channel(key, chans) or find_channel(
            chans, ch_spec["name"], CHANNEL_TEXT, cat["id"])
        if not ch:
            path = f"projects\\{project}" + (f"\\{ch_spec['name']}" if ch_spec["name"] != "general" else "")
            topic = (ch_spec.get("topic", "")
                     .replace("{path}", path).replace("{machine}", MACHINE)
                     .replace("{started}", started))
            ch = create_text_channel(ch_spec["name"], cat["id"], topic)
            chans.append(ch)
            log(f"created channel: #{ch_spec['name']}")
        pin_channel(key, ch["id"],
                    None if ch_spec["name"] == "general" else f"{project}.{ch_spec['name']}")
    return comps


# --- messages -----------------------------------------------------------------

def messages_after(channel_id, after_id, limit=50):
    return api("GET", f"/channels/{channel_id}/messages",
               params={"after": after_id, "limit": limit})


def latest_message_id(channel_id):
    msgs = api("GET", f"/channels/{channel_id}/messages", params={"limit": 1})
    return msgs[0]["id"] if msgs else "0"


def send_message(channel_id, text, files=None):
    """Redacted, chunked send; files attach to the first chunk."""
    text = redact(text or "")
    chunks = chunk_text(text)
    out = []
    for i, piece in enumerate(chunks):
        body = {"content": piece}
        f = [p for p in (files or []) if Path(p).exists()] if i == 0 else None
        out.append(api("POST", f"/channels/{channel_id}/messages", body, files=f))
    return out


IS_VOICE_MESSAGE = 1 << 13   # 8192 - the flag that makes Discord draw a player


def send_voice_message(channel_id, path, duration_secs, waveform):
    """Post ONE Ogg/Opus file as a native voice note - the inline player, not a
    download. `voice.prepare()` makes the three arguments; see its docstring for
    why all three must travel together.

    Shape rules, both from Discord and both silent failures if broken: exactly
    one attachment, and NO content - text belongs in its own message. The
    attachment's `id` is what binds this metadata to `files[0]` in the
    multipart body, so the two indices must stay in step.

    Raises ApiError like any other send. That is the point: the caller's
    fallback to a plain attachment is what keeps an audio reply arriving at all
    if Discord ever refuses voice notes from bots (the docs do not promise it).
    """
    p = Path(path)
    if not p.exists():
        raise ApiError(f"voice note file missing: {p}")
    body = {"flags": IS_VOICE_MESSAGE,
            "attachments": [{"id": 0, "filename": p.name,
                             "duration_secs": float(duration_secs),
                             "waveform": waveform}]}
    return api("POST", f"/channels/{channel_id}/messages", body, files=[str(p)])


OMNIUS_GOLD = 0xC8963E   # the emblem's gold, so fleet posts read as one system


def send_embed(channel_id, title, description="", fields=None, color=OMNIUS_GOLD,
               thumbnail=None, footer=None, message_id=None):
    """Post (or edit) a rich embed. Four documents advertised embeds and the
    pinned #fleet-status board in ARCHITECTURE par.5 depends on them, but no
    line of code implemented one.

    thumbnail: a local file path -> uploaded and referenced as attachment://,
    which is how an embed carries the logo without any external hosting.
    message_id: edit that message instead of posting a new one - a status board
    should be ONE message that updates, not a channel full of snapshots."""
    embed = {"title": redact(title or ""), "color": color}
    if description:
        embed["description"] = redact(description)[:4096]
    if fields:
        # Discord caps at 25 fields; name<=256, value<=1024.
        embed["fields"] = [{"name": redact(str(n))[:256],
                            "value": redact(str(v))[:1024] or "​",
                            "inline": bool(inline)}
                           for n, v, inline in fields[:25]]
    if footer:
        embed["footer"] = {"text": redact(footer)[:2048]}
    files = None
    if thumbnail and Path(thumbnail).exists():
        files = [str(thumbnail)]
        embed["thumbnail"] = {"url": f"attachment://{Path(thumbnail).name}"}
    body = {"embeds": [embed]}
    if message_id:
        return api("PATCH", f"/channels/{channel_id}/messages/{message_id}", body)
    return api("POST", f"/channels/{channel_id}/messages", body, files=files)


def add_reaction(channel_id, message_id, emoji="\N{EYES}"):
    enc = urllib.parse.quote(emoji)
    return api("PUT", f"/channels/{channel_id}/messages/{message_id}/reactions/{enc}/@me")


def trigger_typing(channel_id):
    """Show "Omnius is typing..." in that channel for ~10 seconds.

    Discord expires this on its own, which is the whole reason it was chosen as
    the busy marker over a channel rename or a posted message: if the watchdog
    dies mid-run the indicator is gone within ten seconds, so it can never sit
    there claiming work that stopped. Refresh it while the work continues.
    """
    return api("POST", f"/channels/{channel_id}/typing")


def pin_message(channel_id, message_id, pin=True):
    method = "PUT" if pin else "DELETE"
    return api(method, f"/channels/{channel_id}/pins/{message_id}")


def delete_message(channel_id, message_id):
    return api("DELETE", f"/channels/{channel_id}/messages/{message_id}")


def download(url, dest):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Omnius (watchdog, 1.0)"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        f.write(r.read())
    return dest


# --- CLI ----------------------------------------------------------------------

EMOJI_ALIASES = {"eyes": "\N{EYES}", "check": "\N{WHITE HEAVY CHECK MARK}",
                 "x": "\N{CROSS MARK}", "robot": "\N{ROBOT FACE}"}


def main(argv):
    ap = argparse.ArgumentParser(description="Omnius Discord helper CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ensure", help="stamp schema.json structure (idempotent)")
    p = sub.add_parser("ensure-project", help="stamp a project's category + channels (idempotent)"); p.add_argument("--project", required=True)
    sub.add_parser("channels", help="list categories and channels")
    p = sub.add_parser("send", help="send a message"); p.add_argument("--channel", required=True); p.add_argument("--category"); p.add_argument("--text", required=True); p.add_argument("--file", action="append", default=[])
    p = sub.add_parser("history", help="recent messages"); p.add_argument("--channel", required=True); p.add_argument("--category"); p.add_argument("--limit", type=int, default=10)
    p = sub.add_parser("topic", help="set channel topic"); p.add_argument("--channel", required=True); p.add_argument("--category"); p.add_argument("--text", required=True)
    p = sub.add_parser("react", help="add reaction"); p.add_argument("--channel", required=True); p.add_argument("--category"); p.add_argument("--message", required=True); p.add_argument("--emoji", default="eyes")
    p = sub.add_parser("pin", help="pin a message"); p.add_argument("--channel", required=True); p.add_argument("--category"); p.add_argument("--message", required=True)
    p = sub.add_parser("delete-message", help="delete a message"); p.add_argument("--channel", required=True); p.add_argument("--category"); p.add_argument("--message", required=True)
    p = sub.add_parser("create-channel", help="create text channel"); p.add_argument("--name", required=True); p.add_argument("--category"); p.add_argument("--topic", default="")
    p = sub.add_parser("create-category", help="create category"); p.add_argument("--name", required=True)
    p = sub.add_parser("rename-channel", help="rename channel/category"); p.add_argument("--channel", required=True); p.add_argument("--category"); p.add_argument("--name", required=True)
    p = sub.add_parser("delete-channel", help="delete channel"); p.add_argument("--channel", required=True); p.add_argument("--category")
    p = sub.add_parser("download", help="download attachment url"); p.add_argument("--url", required=True); p.add_argument("--dest", required=True)
    p = sub.add_parser("config-check", help="validate .env Discord values (exit 0 ok, 2 not configured)")
    p.add_argument("--verify", action="store_true", help="also check the token and guild against Discord")
    p = sub.add_parser("set-avatar", help="set the bot's avatar from a PNG/JPG/GIF")
    p.add_argument("--file", default=str(ROOT / "assets" / "omnius.png"))
    p = sub.add_parser("set-name", help="rename the bot (display name only - breaks nothing)")
    p.add_argument("--name", required=True)
    p = sub.add_parser("embed", help="post or edit a rich embed")
    p.add_argument("--channel", required=True); p.add_argument("--category")
    p.add_argument("--title", required=True); p.add_argument("--text", default="")
    p.add_argument("--field", action="append", default=[], metavar="NAME=VALUE")
    p.add_argument("--thumbnail", nargs="?", const=str(ROOT / "assets" / "omnius.png"))
    p.add_argument("--footer"); p.add_argument("--message", help="edit this message instead")
    a = ap.parse_args(argv)

    # config-check must run BEFORE require_config - reporting *why* the config is
    # unusable is the whole point, so it cannot demand a usable config first.
    # This is the one parser the launchers and the services share; PowerShell
    # defers to it so four .env readers can no longer disagree with each other.
    if a.cmd == "config-check":
        problems = config_problems()
        if problems:
            for pb in problems:
                print(f"[X] {pb}")
            return 2
        print("[OK] .env values present and well-formed")
        if not a.verify:
            return 0
        try:
            me = api("GET", "/users/@me")
            print(f"[OK] token valid - bot: {me['username']}")
        except ApiError as e:
            print(f"[X] token rejected by Discord: {e}")
            return 2
        except Exception as e:                      # offline: cannot prove either way
            print(f"[i] could not reach Discord ({type(e).__name__}) - values look right, not verified")
            return 0
        try:
            g = api("GET", f"/guilds/{GUILD}")
            print(f"[OK] bot is in the server: {g['name']}")
        except ApiError:
            print(f"[X] DISCORD_GUILD_ID {GUILD} is not a server this bot is in - "
                  f"check the id and that the bot was invited")
            return 2
        except Exception as e:
            print(f"[i] could not verify the server ({type(e).__name__})")
        # Last, because it is the slowest and the only one a valid token cannot
        # answer. Also the one whose absence you would otherwise discover from
        # blank messages days later.
        ok, why = intent_status()
        if ok is True:
            print(f"[OK] {why}")
        elif ok is False:
            print(f"[X] {why}")
            print("     until then EVERY message arrives empty - no text, no attachments")
            return 2
        else:
            print(f"[i] could not verify Message Content Intent ({why})")
        return 0

    try:
        require_config()
        if a.cmd == "ensure":
            ensure_structure()
            print("structure ok")
        elif a.cmd == "ensure-project":
            comps = ensure_project(a.project)
            print(f"project ok - components: {', '.join(comps) or '(none)'}")
        elif a.cmd == "embed":
            ch = resolve_channel(a.channel, a.category)
            fields = [(n, v, len(v) <= 40) for n, _, v in
                      (f.partition("=") for f in a.field)]
            r = send_embed(ch["id"], a.title, a.text, fields=fields,
                           thumbnail=a.thumbnail, footer=a.footer, message_id=a.message)
            print(f"embed {'edited' if a.message else 'posted'}: {(r or {}).get('id', '')}")
        elif a.cmd == "set-avatar":
            src = Path(a.file)
            if not src.exists():
                print(f"[X] no such file: {src}")
                return 2
            raw = src.read_bytes()
            # Sniff magic bytes, don't ask mimetypes: on Windows it consults the
            # registry, where .png is often mapped to image/x-png - which would
            # make this refuse a perfectly valid PNG on someone else's machine.
            if raw[:8] == b"\x89PNG\r\n\x1a\n":
                mime = "image/png"
            elif raw[:3] == b"\xff\xd8\xff":
                mime = "image/jpeg"
            elif raw[:6] in (b"GIF87a", b"GIF89a"):
                mime = "image/gif"
            else:
                print(f"[X] {src.name} is not a PNG, JPEG or GIF (Discord accepts only those)")
                return 2
            if len(raw) > 10 * 1024 * 1024:
                print(f"[X] {len(raw)/1024/1024:.1f} MB exceeds Discord's 10 MB avatar limit")
                return 2
            # Avatars are sent as a data URI on the user object, not as an upload.
            data_uri = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
            me = api("PATCH", "/users/@me", body={"avatar": data_uri})
            print(f"avatar set for {me['username']} ({len(raw)//1024} KB {mime})")
        elif a.cmd == "set-name":
            # Cosmetic by design: identity is DISCORD_BOT_TOKEN / _GUILD_ID / _OWNER_ID,
            # self-message detection compares user IDs, and the owner allowlist is an
            # id too. Nothing in the bus matches the bot by name. Each instance gets
            # its own bot, so a fresh machine will want this.
            before = api("GET", "/users/@me")["username"]
            me = api("PATCH", "/users/@me", body={"username": a.name})
            print(f"bot renamed: {before} -> {me['username']}")
        elif a.cmd == "channels":
            cats = {c["id"]: c["name"] for c in guild_channels() if c["type"] == CHANNEL_CATEGORY}
            for c in sorted(guild_channels(), key=lambda x: (x.get("parent_id") or "", x["position"])):
                if c["type"] == CHANNEL_TEXT:
                    print(f"{cats.get(c.get('parent_id'), '-'):24} #{c['name']:20} {c['id']}")
        elif a.cmd == "send":
            ch = resolve_channel(a.channel, a.category)
            send_message(ch["id"], a.text, files=a.file)
            print("sent")
        elif a.cmd == "history":
            ch = resolve_channel(a.channel, a.category)
            for m in reversed(api("GET", f"/channels/{ch['id']}/messages", params={"limit": a.limit})):
                print(f"[{m['timestamp'][:16]}] {m['author']['username']}: {m['content'][:120]}")
        elif a.cmd == "topic":
            set_topic(resolve_channel(a.channel, a.category)["id"], a.text); print("ok")
        elif a.cmd == "react":
            add_reaction(resolve_channel(a.channel, a.category)["id"], a.message,
                         EMOJI_ALIASES.get(a.emoji, a.emoji)); print("ok")
        elif a.cmd == "pin":
            pin_message(resolve_channel(a.channel, a.category)["id"], a.message); print("ok")
        elif a.cmd == "delete-message":
            delete_message(resolve_channel(a.channel, a.category)["id"], a.message); print("ok")
        elif a.cmd == "create-channel":
            parent = None
            if a.category:
                parent = find_channel(guild_channels(), a.category, CHANNEL_CATEGORY)
                if not parent:
                    parent = create_category(a.category)
            ch = create_text_channel(a.name, parent["id"] if parent else None, a.topic)
            print(ch["id"])
        elif a.cmd == "create-category":
            print(create_category(a.name)["id"])
        elif a.cmd == "rename-channel":
            rename_channel(resolve_channel(a.channel, a.category)["id"], a.name); print("ok")
        elif a.cmd == "delete-channel":
            delete_channel(resolve_channel(a.channel, a.category)["id"]); print("ok")
        elif a.cmd == "download":
            print(download(a.url, a.dest))
    except ApiError as e:
        print(f"[X] {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
