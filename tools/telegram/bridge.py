#!/usr/bin/env python3
"""Telegram <-> one Discord channel, for people who have no Discord account.

    python tools\\telegram\\bridge.py           run it (the watchdog does this for you)
    python tools\\telegram\\bridge.py --once    one pass, then exit (for testing)
    python tools\\telegram\\bridge.py --check   report the config and leave

WHAT IT IS FOR: inviting one person - a client, a colleague - into exactly ONE
channel, without giving them a Discord account, a desk, or any control over the
fleet. They write to a Telegram bot; the message appears in that channel
attributed to them; the desk answers as it answers anyone; the answer goes back.

WHY IT NEEDS NO CHANGES TO THE CORE, which is the whole design:

  * A message posted through the bot token is ignored by the watchdog at its
    first branch (author.bot -> "skip-bot", watchdog.py). So this can mirror
    into Discord without the watchdog delivering it twice.
  * Dropping a file into state\\inbox\\<desk>\\ starts that desk within ~3s
    (watchdog.ensure_runners). No signal, no import, no API.
  * A desk decides who it is talking to from the envelope's `from` label, not
    from any Discord account (watchdog.is_human_sender). Anything that is not a
    fleet tag is treated as a guest already - so an invited Telegram user gets
    correct guest treatment for free, and config\\guests.ini stays what it is:
    the gate for people who DO have Discord accounts.

WHAT THE INVITED PERSON CANNOT DO, enforced in the watchdog rather than here:
control verbs, slash commands, answering a permission / takeover / gate / 2FA
prompt - all of those live inside `if sender == "owner":`. And enforced here:
reaching any channel but their own, or being heard at all if their id is not in
config\\telegram.ini.

Stdlib only (urllib), like every other tool here: it must keep working on a
fresh install with no pip step.
"""
import argparse
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
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tools" / "discord"))
import omnius_config as ocfg  # noqa: E402

API = "https://api.telegram.org"
TIMEOUT = 40.0                 # long-poll needs to outlive POLL_WAIT
POLL_WAIT = 25                 # getUpdates long-poll seconds
MIRROR_SECONDS = 5.0           # Discord -> Telegram pass cadence
IDLE_SECONDS = 30.0            # unconfigured: sleep, NEVER exit (see below)
STATE = ROOT / "state" / "telegram"
TRANSCRIPTS = ROOT / "state" / "transcripts"
MEDIA_IN = ROOT / "media" / "inbox"
MAX_TG_UPLOAD = 45 * 1024 * 1024
# How long an unproven lock is respected before it is treated as stale.
# Long enough for a real bridge to boot and stamp its first beacon,
# short enough that a pid recycled across a reboot cannot lock Telegram
# out of the machine for good.
LOCK_GRACE_SECONDS = 900.0
LOG = ROOT / "state" / "logs" / "telegram.log"


class TelegramError(Exception):
    """Something the owner should see. Mapped to exit 1 by main()."""


# --- one bridge at a time -----------------------------------------------------
#
# Telegram allows ONE getUpdates consumer per bot: a second poller makes both
# sides fight, each getting half the messages or a 409. The watchdog starts this
# process, but so can a human, a leftover scheduled task, or a second workspace
# on the same machine - so the guarantee belongs HERE, not in whoever launches
# it. Identical shape to watchdog.acquire_lock (pid + image name, because a pid
# is not an identity: Windows reuses numbers, and a saved pid across a reboot
# very likely names something else).

def _process_image(pid):
    """-> the exe filename behind a pid (lowercased), or None."""
    import ctypes
    try:
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return None
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(1024)
        ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf,
                                                               ctypes.byref(size))
        ctypes.windll.kernel32.CloseHandle(h)
        return Path(buf.value).name.lower() if ok else None
    except Exception:                                            # noqa: BLE001
        return None


def pid_alive(pid, expect="python"):
    """Is this pid alive AND still the kind of process we recorded?"""
    if not pid:
        return False
    import ctypes
    try:
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        if not (bool(ok) and code.value == 259):                 # STILL_ACTIVE
            return False
    except Exception:                                            # noqa: BLE001
        return False
    if not expect:
        return True
    img = _process_image(pid)
    # Unreadable image -> trust liveness rather than declare a live bridge dead.
    return img is None or expect in img


def lock_path(token=""):
    """Where the one-bridge-per-BOT lock lives.

    Keyed by the token, and kept OUTSIDE the workspace, because that is what is
    actually being protected: Telegram allows one getUpdates consumer per bot,
    and two workspaces on one machine (an unzipped backup, a test clone) share
    the machine, not the folder. A lock under state\\ could not see across them
    and the comment above claimed it could.

    The token is hashed, never written: a lock file is not a place for a bearer
    credential, and the hash is all that is needed to tell two bots apart.
    """
    import hashlib
    if not token:
        return STATE / "lock.json"             # no token yet: workspace-local
    tag = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) / "omnius" / f"telegram-{tag}.lock") if base \
        else STATE / f"lock-{tag}.json"


def acquire_lock(token=""):
    """-> True if we own the bridge, False if another live one already does."""
    try:
        lock = lock_path(token)
        lock.parent.mkdir(parents=True, exist_ok=True)
        if lock.exists():
            try:
                old = json.loads(lock.read_text(encoding="utf-8"))
                if int(old.get("pid") or 0) != os.getpid() and pid_alive(old.get("pid")):
                    # Alive is not proof. Windows recycles pids across a reboot,
                    # and this lock survives one - so a stranger's python can
                    # inherit the number and lock Telegram out of the machine
                    # forever. Only a beacon stamped AFTER the lock proves the
                    # process behind it is really a bridge; without that proof
                    # the lock expires.
                    started = float(old.get("startedTs") or 0)
                    try:
                        proven = (STATE / "beacon.json").stat().st_mtime > started > 0
                    except OSError:
                        proven = False
                    if proven or time.time() - started < LOCK_GRACE_SECONDS:
                        log(f"another telegram bridge is already running "
                            f"(pid {old['pid']}) - leaving it alone")
                        return False
                    log(f"the lock names pid {old['pid']}, which has never stamped "
                        f"a beacon - treating it as stale and taking over")
            except (ValueError, OSError):
                pass                                    # torn or garbled = stale
        lock.write_text(json.dumps(
            {"pid": os.getpid(), "startedAt": utc_now(),
             "startedTs": time.time(), "root": str(ROOT)}),
            encoding="utf-8")
        # The workspace-local copy is what the WATCHDOG reads: it supervises the
        # bridge in its own tree and must not be able to see, or kill, a bridge
        # belonging to another workspace.
        try:
            STATE.mkdir(parents=True, exist_ok=True)
            (STATE / "lock.json").write_text(lock.read_text(encoding="utf-8"),
                                             encoding="utf-8")
        except OSError:
            pass
        return True
    except OSError as e:
        log(f"could not take the bridge lock: {e}")
        return True         # a lock we cannot write must not stop the bridge


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    """One line, timestamped, token never included - see _safe()."""
    # UTC, like every other stamp in this tree (watchdog.now_iso). It was
    # datetime.now() with a Z glued on, which reads as UTC and is not - two
    # hours out in summer, and the one thing a log must never lie about is when.
    line = f"{utc_now()} {_safe(str(msg))}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        if LOG.exists() and LOG.stat().st_size > 2_000_000:      # same budget as the watchdog
            LOG.replace(LOG.with_suffix(".log.1"))
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


_TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")


def _safe(text):
    """A bot token is a bearer credential and this log is a file on disk."""
    return _TOKEN_RE.sub("<TELEGRAM_TOKEN>", text or "")


# --- config -------------------------------------------------------------------

def load_config():
    """-> (token, mirror_seconds, {label: chat}). Never raises.

    Missing config is a NORMAL state, not an error: the bridge idles instead of
    exiting, because the watchdog restarts it whenever it is gone (checked every
    minute) - so a process that exits on purpose is indistinguishable from one
    that crashed, and gets relaunched forever. That boot-loop shape cost 112
    relaunches on 2026-08-18; it is not repeated here.
    """
    cfg = ocfg.load("telegram")
    section = cfg.get("telegram") or {}
    env_key = str(section.get("token_env") or "TELEGRAM_BOT_TOKEN").strip()
    token = ocfg.env_value(env_key)
    try:
        mirror = float(str(section.get("mirror_seconds") or MIRROR_SECONDS))
    except ValueError:
        mirror = MIRROR_SECONDS
    return token, max(2.0, mirror), ocfg.telegram_chats()


# --- telegram ------------------------------------------------------------------

def tg(token, method, params=None, files=None, timeout=TIMEOUT):
    """One Telegram Bot API call. -> the `result` payload.

    Retries the way api.py does - the same failures, the same reasoning: a
    transient 5xx or a dropped socket must not end the process, and a rate
    limit must not be able to sleep the bridge for an unbounded time.
    """
    url = f"{API}/bot{token}/{method}"
    for attempt in range(4):
        try:
            if files:
                body, ctype = _multipart(params or {}, files)
                req = urllib.request.Request(url, data=body,
                                             headers={"Content-Type": ctype})
            else:
                data = urllib.parse.urlencode(params or {}, doseq=True).encode()
                req = urllib.request.Request(url, data=data)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                out = json.loads(r.read().decode("utf-8", "replace") or "{}")
            if not out.get("ok"):
                raise TelegramError(f"{method}: {out.get('description')}")
            return out.get("result")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code == 429:
                try:
                    wait = min(int(json.loads(detail)["parameters"]["retry_after"]), 60)
                except Exception:                                # noqa: BLE001
                    wait = 5
                log(f"telegram rate limit on {method} - waiting {wait}s")
                time.sleep(wait)
                continue
            if e.code >= 500 and attempt < 3:
                time.sleep(1 + attempt)
                continue
            raise TelegramError(f"{method} -> HTTP {e.code}: {_safe(detail)}")
        except urllib.error.URLError as e:
            if attempt < 3:
                time.sleep(2 + attempt)
                continue
            raise TelegramError(f"cannot reach telegram: {e.reason}")
    raise TelegramError(f"{method}: gave up after retries")


def _multipart(fields, files):
    """Hand-rolled, same shape api.py uses - no third-party dependency."""
    boundary = f"----OmniusTG{int(time.time() * 1000)}"
    out = bytearray()
    for k, v in fields.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n"
                f"{v}\r\n").encode()
    for field, path in files:
        p = Path(path)
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; "
                f"filename=\"{p.name}\"\r\nContent-Type: {ctype}\r\n\r\n").encode()
        out += p.read_bytes() + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def tg_download(token, file_id, dest):
    """Telegram hands out a path, then serves the bytes from a second host."""
    info = tg(token, "getFile", {"file_id": file_id})
    src = f"{API}/file/bot{token}/{info['file_path']}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(src, headers={"User-Agent": "omnius-telegram"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as fh:
        fh.write(r.read())
    return dest


# --- cursors (ours alone; state\watchdog\last_ids.json belongs to the watchdog) --

def _cursor_path(name):
    return STATE / f"{name}.json"


def read_cursor(name, default=None):
    try:
        return json.loads(_cursor_path(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {} if default is None else default


def write_cursor(name, data):
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        p = _cursor_path(name)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(p)
    except OSError as e:
        log(f"cursor write failed ({name}): {e}")


# --- the two directions ---------------------------------------------------------

def sanitize(name):
    """Same rule watchdog.save_attachments uses, so the archive stays uniform."""
    keep = "".join(c for c in (name or "file") if c.isalnum() or c in "._-")
    return keep or "file"


def describe_attachments(msg):
    """Names, not bytes - what an invited person sees while media_out is off."""
    names = [a.get("filename") or "file" for a in (msg.get("attachments") or [])]
    return f"\n[{len(names)} attachment(s): {', '.join(names)}]" if names else ""


_CHANNELS = {}
_DESKS = {}


def desk_for_channel(api, cid, name=None):
    """Which desk answers this channel? -> desk id, or None.

    THE CHANNEL ALREADY KNOWS. `#web` in the `my-project` category is the
    `my-project.web` desk; that map is how every message the owner types finds
    its session, and config\\telegram.ini asking for it a second time was just
    a chance to disagree with the fleet. So `desk` is optional now, and this is
    the same function the watchdog uses - imported, not reimplemented, because
    two copies of a mapping is how they drift.
    """
    key = str(cid)
    if key in _DESKS:
        return _DESKS[key]
    session = None
    try:
        import watchdog as wd                  # lazy: --check must not need it
        for chan_id, target in wd.build_map(api.load_schema()).items():
            if str(chan_id) == key:
                session = target.session
                break
    except Exception as e:                                       # noqa: BLE001
        # RAISED, not swallowed into None. A Discord hiccup here used to read as
        # "this channel answers to no desk", which drops the message on purpose
        # and counts it as handled. Failing loudly lets inbound retry it.
        raise TelegramError(f"could not resolve the desk for "
                            f"#{name or key}: {type(e).__name__}: {e}")
    if session:
        _DESKS[key] = session
    # A NEGATIVE answer is never cached: "this channel has no desk" is true only
    # of the fleet as it stands, and creating that project is exactly what the
    # owner does next. Caching it would make him restart the bridge to be heard.
    return session


def channel_of(api, chat):
    """-> (id, name, category) for a configured channel. Cached per process.

    Config may name a channel (`#general`) or give its id. An id is one dict and
    no round trip; a name costs one guild listing, once. The name and category
    are carried into the envelope for the same reason the watchdog carries them:
    every project has a #general, so without the category a desk cannot tell
    WHICH project it is being asked about.
    """
    key = str(chat["discord_channel"])
    if key in _CHANNELS:
        return _CHANNELS[key]
    ch = api.resolve_channel(key)
    name, category = ch.get("name"), None
    if not name:                                # config gave an id: fill in the rest
        for c in api.guild_channels():
            if str(c["id"]) == str(ch["id"]):
                ch = c
                name = c.get("name")
                break
    parent = ch.get("parent_id")
    if parent:
        for c in api.guild_channels():
            if str(c["id"]) == str(parent):
                category = c.get("name")
                break
    _CHANNELS[key] = (str(ch["id"]), name, category)
    return _CHANNELS[key]


def transcribe_line(api, desk, direction, text, channel=None, channel_id=None,
                    files=None, who=None):
    """Append to the desk's bus transcript, exactly as the watchdog does.

    Deliberately duplicated rather than imported: importing watchdog.py would
    pull the whole always-on brain into this process. Eight lines of JSONL is
    the cheaper coupling, and without them a Telegram conversation would be
    missing from !trace and from the history a later run reads back.
    """
    try:
        d = TRANSCRIPTS / desk
        d.mkdir(parents=True, exist_ok=True)
        line = {"ts": utc_now(),
                "dir": direction, "channel": channel, "channelId": channel_id,
                "text": api.redact(text or ""),
                "files": [Path(p).name for p in (files or [])]}
        if who:
            line["from"] = who
        with open(d / f"{datetime.now().strftime('%Y-%m')}.jsonl", "a",
                  encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError) as e:
        log(f"transcript write failed for {desk}: {e}")          # never fatal


def open_reply_window(label, cid):
    """Mark that this person just spoke, so the desk's answer may reach them.

    Written into the mirror cursor because that is where the mirror reads it,
    and inbound always runs before outbound in a pass - so there is one writer
    at a time and no lock is needed.
    """
    cur = read_cursor("mirror", {})
    cur[f"await:{label}@{cid}"] = time.time()
    write_cursor("mirror", cur)


def _handle(text):
    """A Telegram handle reduced to what it can safely be: [a-z0-9_]."""
    return re.sub(r"[^a-z0-9_]", "", str(text or "").lower())[:32]


def match_chat(chats, msg, api):
    """Which invited person wrote this? -> label, or None.

    Three ways, in order, and the order is the security:

    1. a numeric telegram_user_id in the config - exact, unforgeable;
    2. an id PINNED here on first contact - see below;
    3. a telegram_username from the config, matched once, then pinned.

    The pin is why usernames are safe enough to invite with. A Telegram handle
    can be released and re-registered by somebody else; an invite that matched
    on the handle forever would follow it to that stranger. Matching once and
    then remembering the numeric id behind it means the handle is only ever a
    way of SAYING who you mean - after the first message it stops mattering.
    """
    uid = str((msg.get("from") or {}).get("id") or "")
    if not uid:
        return None
    for label, c in chats.items():
        if c.get("telegram_user_id") == uid:
            return label
    pins = read_cursor("pinned", {})
    for label in chats:
        if pins.get(label) == uid:
            return label
    handle = _handle((msg.get("from") or {}).get("username"))
    if not handle:
        return None
    for label, c in chats.items():
        if c.get("telegram_username") == handle and label not in pins:
            pins[label] = uid
            write_cursor("pinned", pins)
            log(f"{label} (@{handle}) is telegram id {uid} - pinned to this invite")
            _say_in_discord(api, c, f"**{label}** (telegram, @{handle}) is connected.")
            return label
    return None


STRANGER_CAP = 5            # new ids announced per run; the rest only logged


def announce_stranger(api, uid, username):
    """Tell the OWNER, in Discord, that someone unlisted wrote to the bot.

    Not the stranger - they still get silence. This exists because the only
    other way to learn a person's numeric id was to open state\\logs\\, which
    is not something to ask of someone running his fleet from a phone.

    Their message is NOT included: it is untrusted text, and the id plus handle
    is all that is needed to decide whether to invite them. Once per id, capped,
    and the handle is stripped to [a-z0-9_] so nothing in it can mention anyone.
    """
    seen = read_cursor("strangers", {"ids": []})
    ids = list(seen.get("ids") or [])
    if uid in ids:
        return
    ids.append(uid)
    write_cursor("strangers", {"ids": ids[-200:]})
    handle = _handle(username)
    log(f"ignored a message from telegram id {uid} - not in config\\telegram.ini")
    if len(ids) > STRANGER_CAP:
        return                                  # a flood stays in the log only
    who = f"@{handle} (id `{uid}`)" if handle else f"telegram id `{uid}`"
    # A fleet channel, never a guest's: resolved through the api object we were
    # handed (so tests can hand us a fake), never by importing the watchdog.
    for name in ("alerts", "omnius", "orchestrator"):
        try:
            cid = str(api.resolve_channel(name)["id"])
        except Exception:                                        # noqa: BLE001
            continue
        try:
            api.send_message(cid,
                f"📨 {who} wrote to the Telegram bot and is **not invited**. "
                f"To let them in, add to `config\\telegram.ini`:\n"
                f"```ini\n[chat.{handle or 'their-name'}]\n"
                f"telegram_user_id = {uid}\n"
                f"discord_channel  = <the channel they should reach>\n```")
        except Exception as e:                                   # noqa: BLE001
            log(f"could not report the unlisted sender in Discord: "
                f"{type(e).__name__}: {e}")
        return
    log(f"no alerts/omnius channel to report the unlisted sender in")


def _say_in_discord(api, chat, text):
    try:
        cid, _n, _c = channel_of(api, chat)
        api.send_message(cid, text)
    except Exception as e:                                       # noqa: BLE001
        log(f"could not post to #{chat.get('discord_channel')}: {type(e).__name__}: {e}")


def inbound(token, chats, api):
    """Telegram -> Discord -> the desk's inbox. -> number of messages relayed."""
    cur = read_cursor("updates", {})
    offset = int(cur.get("offset") or 0)
    updates = tg(token, "getUpdates", {"offset": offset, "timeout": POLL_WAIT,
                                       "allowed_updates": json.dumps(["message"])},
                 timeout=POLL_WAIT + 15) or []
    misses = dict(cur.get("misses") or {})
    # The cursor is written in a finally for the same reason the mirror's is:
    # one unexpected exception used to discard every advance made in the pass,
    # so Telegram re-sent messages that had already reached Discord.
    state = {"offset": offset, "relayed": 0}
    try:
        _drain(token, chats, api, updates, misses, state)
    finally:
        write_cursor("updates", {"offset": state["offset"], "misses": misses})
    return state["relayed"]


def _drain(token, chats, api, updates, misses, state):
    for upd in updates:
        uid_num = int(upd.get("update_id", 0))
        msg = upd.get("message") or {}
        uid = str((msg.get("from") or {}).get("id") or "")
        chat_id = str((msg.get("chat") or {}).get("id") or "")
        who = match_chat(chats, msg, api)
        if not who:
            # Silence towards THEM is deliberate - an explanatory refusal tells a
            # stranger something is here. The owner, though, is told in Discord:
            # that is where he is, and the alternative was reading a log file.
            announce_stranger(api, uid, (msg.get("from") or {}).get("username"))
            state["offset"] = max(state["offset"], uid_num + 1)
            continue
        if (msg.get("text") or "").strip().split()[:1] == ["/start"]:
            # Telegram's handshake, not a message for anyone. Relaying it would
            # spend a whole desk run on the word "/start"; ignoring it silently
            # would leave the person tapping Start and wondering. So: answer
            # here, in Telegram, and stop.
            try:
                tg(token, "sendMessage",
                   {"chat_id": uid,
                    "text": "Connected. Write here and Omnius will answer."})
            except TelegramError as e:
                log(f"could not greet {who}: {e}")
            state["offset"] = max(state["offset"], uid_num + 1)
            continue
        # Stamped BEFORE the slow part, and again after each message. One pass
        # can legitimately block for many minutes - a voice note gives whisper
        # up to 600s - and a beacon written only at the end of the pass made the
        # watchdog read that as wedged, kill the bridge mid-transcription, and
        # (since the offset had not moved) fetch the same voice note again on
        # restart. A poison pill assembled from three reasonable decisions.
        _beacon(len(chats), "working")
        try:
            state["relayed"] += _relay_one(token, api, who, chats[who], msg, chat_id)
            state["offset"] = max(state["offset"], uid_num + 1)
            misses.pop(str(uid_num), None)
        except Exception as e:                                   # noqa: BLE001
            # The offset does NOT advance past a message we failed to deliver.
            # Advancing was silent loss: Telegram treats a confirmed offset as
            # delivered and never sends that update again, so a Discord 503 meant
            # the guest saw "sent" and got silence - with nothing in the channel
            # for the owner to notice either.
            #
            # But a message that can NEVER be relayed would then block everything
            # queued behind it, so it is retried twice and then let go, loudly.
            # Same bargain the mirror makes in the other direction.
            n = int(misses.get(str(uid_num)) or 0) + 1
            misses[str(uid_num)] = n
            log(f"could not relay from {who} (try {n}/3): {type(e).__name__}: {e}")
            if n >= 3:
                state["offset"] = max(state["offset"], uid_num + 1)
                misses.pop(str(uid_num), None)
                log(f"giving up on that message from {who} - nothing of it "
                    f"reached Discord and it will not be retried")
            break                 # keep their messages in order: retry this one


def _relay_one(token, api, label, chat, msg, tg_chat_id):
    """One Telegram message -> a Discord post + an envelope for the desk."""
    text = (msg.get("text") or msg.get("caption") or "").strip()
    files, notes = [], []
    month = datetime.now().strftime("%Y-%m")
    mid = msg.get("message_id")

    # Resolved BEFORE anything is downloaded or posted: a message with nowhere
    # to go should cost nothing and must not appear in the channel as if it had
    # been handled.
    cid, cname, category = channel_of(api, chat)
    desk = chat.get("desk") or desk_for_channel(api, cid, cname)
    if not desk:
        log(f"#{cname or cid} answers to no desk, so {label}'s message has nowhere "
            f"to go - either invite them to a desk's own channel, or name one with "
            f"desk = ... in config\\telegram.ini")
        return 0

    media = None
    if msg.get("photo"):                       # sizes ascending; the last is the biggest
        media = (msg["photo"][-1]["file_id"], f"photo-{mid}.jpg", "image/jpeg")
    elif msg.get("voice"):
        media = (msg["voice"]["file_id"], f"voice-{mid}.ogg", "audio/ogg")
    elif msg.get("audio"):
        a = msg["audio"]
        media = (a["file_id"], a.get("file_name") or f"audio-{mid}.mp3", "audio/mpeg")
    elif msg.get("document"):
        d = msg["document"]
        media = (d["file_id"], d.get("file_name") or f"file-{mid}",
                 d.get("mime_type") or "application/octet-stream")
    elif msg.get("video"):
        media = (msg["video"]["file_id"], f"video-{mid}.mp4", "video/mp4")

    if media:
        file_id, name, ctype = media
        dest = MEDIA_IN / month / f"tg{mid}-{sanitize(name)}"
        tg_download(token, file_id, dest)
        files.append({"path": str(dest), "name": dest.name, "type": ctype})
        if msg.get("voice"):
            _beacon(None, "working")             # whisper can hold this for minutes
            spoken = transcribe(dest)
            if spoken:
                notes.append(f"🎙 {spoken}")
                text = (text + "\n" + spoken).strip() if text else spoken

    body = f"**{label}** (telegram): {text}" if text else f"**{label}** (telegram) sent a file"
    if notes and not text:
        body = f"**{label}** (telegram): " + " ".join(notes)
    # ONE CHUNK PER CALL, remembered as it lands. api.send_message posts several
    # messages for a long text and raises on the one that fails - so a failure
    # halfway used to leave earlier chunks in the channel with nothing recording
    # them, and the retry posted them again while the orphans were mirrored back
    # to their own author.
    ids = []
    for i, piece in enumerate(api.chunk_text(api.redact(body))):
        posted = api.send_message(cid, piece,
                                  files=[f["path"] for f in files] if i == 0 else None)
        for p in (posted or []):
            if p and p.get("id"):
                ids.append(str(p["id"]))
                remember_relay(str(p["id"]), label)
    discord_id = ids[0] if ids else ""
    if not discord_id:
        raise TelegramError("Discord accepted the post but returned no message id")

    # The envelope id IS the Discord message id: inbox names sort
    # lexicographically, so a prefixed stem would queue behind every snowflake -
    # and sharing the id makes the Discord message, the envelope, the transcript
    # and !trace one story.
    box = ROOT / "state" / "inbox" / desk
    box.mkdir(parents=True, exist_ok=True)
    envelope = {"id": discord_id, "from": label, "channel": cname,
                "channelId": cid, "category": category,
                "ts": utc_now(),
                "text": text, "files": files}
    # Atomic, like every other envelope writer: ensure_runners scans this folder
    # every 3 seconds and a half-written file would be read as a torn envelope.
    tmp = box / f"{discord_id}.json.tmp"
    tmp.write_text(json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(box / f"{discord_id}.json")
    transcribe_line(api, desk, "in", text, channel=cname, channel_id=cid,
                    files=[f["path"] for f in files], who=label)

    # EVERY chunk, not just the first. api.send_message splits at Discord's
    # 2000-char limit and returns one id per chunk; remembering only ids[0] left
    # the tail of a long message looking like an ordinary bot post, so it was
    # mirrored straight back to the person who wrote it.
    open_reply_window(label, cid)
    log(f"{label} -> #{cname or cid} (desk {desk}, {len(files)} file(s))")
    return 1


# Ids kept. Sized against the OTHER direction: inbound takes up to 100 updates a
# pass while the mirror reads at most 50 messages, so the relayed-but-not-yet-
# mirrored set can grow ~50 a pass. At 500 a burst could prune an id while it was
# still ahead of the cursor - and a forgotten id is echoed back to its own author
# as "Omnius: **antonio** (telegram): ...". 5000 is ~100 passes of headroom and
# costs a few hundred KB of JSON.
RELAY_MEMORY = 5000


def remember_relay(discord_id, label):
    """Record WHOSE message we posted, not merely that we posted one.

    A bare list of ids was enough while one channel held one guest. With two
    people in the same channel it silently hid them from each other: every
    relayed message looked like "ours" to every chat. Keeping the label means a
    person's own words are skipped for them (no echo) while still reaching
    anyone else in that room who is allowed to see the traffic.
    """
    by = dict((read_cursor("posted", {"by": {}}) or {}).get("by") or {})
    by[str(discord_id)] = label
    if len(by) > RELAY_MEMORY:
        for k in sorted(by, key=_id_key)[:len(by) - RELAY_MEMORY]:
            del by[k]
    write_cursor("posted", {"by": by})


def _id_key(mid):
    try:
        return int(mid)
    except (TypeError, ValueError):
        return 0


def _strip_relay_prefix(content, label):
    """Undo our own "**antonio** (telegram): " marker before echoing it on.

    Someone reading this room in Telegram gets the name from the line we build,
    so leaving the Discord markup in would read as "antonio (telegram):
    **antonio** (telegram): hola".
    """
    text = (content or "").strip()
    for marker in (f"**{label}** (telegram): ", f"**{label}** (telegram) "):
        if text.startswith(marker):
            return text[len(marker):].strip()
    return text


def transcribe(path):
    """Voice note -> text, via the tool that already does it. Never fatal."""
    import subprocess
    try:
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "whisper" / "transcribe.py"),
                            str(path)], capture_output=True, text=True, timeout=600,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return (r.stdout or "").strip() if r.returncode == 0 else ""
    except Exception as e:                                       # noqa: BLE001
        log(f"transcription failed for {Path(path).name}: {type(e).__name__}")
        return ""


def outbound(token, chats, api, me_id):
    """Discord -> Telegram. -> number of messages mirrored."""
    cur = read_cursor("mirror", {})
    relayed = (read_cursor("posted", {"by": {}}) or {}).get("by") or {}
    sent = 0
    try:
        sent = _mirror(token, chats, api, me_id, cur, relayed)
    finally:
        # In a finally, so an escape on any path still persists what was already
        # delivered. Without it one unexpected exception re-sent every message
        # Telegram had already accepted this pass.
        write_cursor("mirror", cur)
    return sent


def _mirror(token, chats, api, me_id, cur, relayed):
    sent = 0
    rooms = {}
    for label, chat in chats.items():
        try:
            rooms.setdefault(channel_of(api, chat)[0], []).append(label)
        except Exception:                                        # noqa: BLE001
            pass
    for label, chat in chats.items():
        # Stamped per chat: one mirror pass can be slow (a big catch-up, a
        # rate limit), and a beacon written only at the end of both directions
        # made that look wedged.
        _beacon(None, "working")
        try:
            cid, _cname, _cat = channel_of(api, chat)
        except Exception as e:                                   # noqa: BLE001
            log(f"{label}: channel {chat['discord_channel']} not found - {e}")
            continue
        # PER PERSON, not per channel. Keyed by channel alone, the first label in
        # the loop advanced the cursor past every message and everyone else
        # sharing that room got an empty read - permanently. The shared-room
        # feature was documented, tested, and silently one-way.
        key = f"{label}@{cid}"
        if key not in cur and cid in cur:
            # Upgrading from the version that keyed this by channel alone. Left
            # unmigrated, the new key looks like a first run and jumps to NOW -
            # silently swallowing whatever the desk said while the update ran.
            cur[key] = cur.pop(cid)
            cur.pop(f"stuck:{cid}", None)
        if chat["visibility"] == "own" and len(rooms.get(cid, [])) > 1:
            # config caught this when both blocks named the channel the same
            # way; here the names are RESOLVED, so an id in one block and a
            # #name in the other is caught too. Fail closed: a promise of
            # privacy that cannot be kept is not narrowed, it is stopped.
            if f"shared:{key}" not in cur:
                cur[f"shared:{key}"] = 1
                log(f"{label}: visibility=own but #{chat['discord_channel']} is "
                    f"shared with {len(rooms[cid]) - 1} other invited person(s) - "
                    f"mirroring nothing to them until that is resolved "
                    f"(give them their own channel, or set visibility=all)")
            continue
        last = cur.get(key)
        try:
            if not last:
                # First run: start from NOW. Replaying a channel's history into
                # someone's phone the moment they are invited would be a
                # surprise, and under visibility=own it would be a leak.
                cur[key] = str(api.latest_message_id(cid))
                continue
            msgs = sorted(api.messages_after(cid, last), key=lambda m: int(m["id"]))
        except Exception as e:                                   # noqa: BLE001
            log(f"mirror read failed for #{cid}: {type(e).__name__}: {e}")
            continue
        for m in msgs:
            mid = str(m["id"])
            author = m.get("author") or {}
            is_bot = bool(author.get("bot")) or str(author.get("id")) == str(me_id)
            relay_for = relayed.get(mid)       # None, or whose message we posted
            if not is_bot:
                # Somebody else is talking now: whatever the desk says next is
                # answering THEM, so this person's reply window closes.
                cur.pop(f"await:{key}", None)
            if relay_for == label:
                # Seeing their own message go by re-opens the window: order in
                # the channel is what decides who the desk is answering, and the
                # stamp written at relay time cannot know what came after it.
                cur[f"await:{key}"] = time.time()
                cur[key] = mid
                continue                       # their own words back - never loop
            if chat["visibility"] == "own" and not _is_their_answer(
                    m, is_bot, relay_for, cur, key):
                cur[key] = mid
                continue                       # own = their messages + THEIR answers
            if relay_for:
                # Another invited person in the same room. Only reachable under
                # visibility=all, and attributed to THEM rather than to Omnius,
                # which is what posted it.
                name, text = f"{relay_for} (telegram)", _strip_relay_prefix(
                    m.get("content"), relay_for)
            else:
                name = "Omnius" if is_bot else (author.get("global_name")
                                                or author.get("username") or "owner")
                text = (m.get("content") or "").strip()
            body = f"{name}: {text}" if text else f"{name}:"
            body += describe_attachments(m)
            files = _fetch_for_telegram(api, m) if chat.get("media_out") else []
            try:
                _send_to_telegram(token, chat["telegram_user_id"], body, files)
                cur[key] = mid                 # ONLY on success - see below
                cur.pop(f"stuck:{key}", None)
                sent += 1
            except TelegramError as e:
                # The cursor does NOT advance on failure, so a Telegram outage
                # is retried rather than silently swallowing what the desk said.
                # But a permanent refusal (they blocked the bot) would then wedge
                # the mirror forever, so give up after three passes and SAY SO -
                # a dropped message that nobody is told about is the worst of
                # both designs.
                stuck = int(cur.get(f"stuck:{key}") or 0) + 1
                cur[f"stuck:{key}"] = stuck
                log(f"mirror to {label} failed ({stuck}/3): {e}")
                if stuck >= 3:
                    cur[key] = mid
                    cur.pop(f"stuck:{key}", None)
                    log(f"gave up mirroring message {mid} to {label} - skipped it")
                break                          # keep order: retry this one first
            except Exception as e:                               # noqa: BLE001
                # NOT just TelegramError. A socket timeout inside r.read(), a
                # non-JSON body, an OSError reading a file that was cleaned up -
                # none of those are TelegramError, and letting one escape here
                # discarded every cursor advance made this pass, re-sending
                # messages Telegram had already accepted.
                log(f"mirror to {label} failed: {type(e).__name__}: {e}")
                break
    return sent


# Posts the FLEET makes about itself. Never forwarded to an invited person under
# visibility=own, whatever else is true: a permission ask, a takeover ask, a
# 2FA prompt, a delegation mirror or a deaf-desk alarm is machine business, and
# some of them carry codes. Matched on the markers the watchdog actually uses.
FLEET_MARKERS = ("wants a 6-digit code", "[desk mail]", "needs permission",
                 "wants to run", "ok/no", "takeover", "hop budget", "routine `",
                 "⚠", "🔐", "🔑", "⏱", "📨", "🛠", "🧭")


def _is_their_answer(m, is_bot, relay_for, cur, key):
    """Under visibility=own, may this bot message go to that person? -> bool.

    `own` promises "your messages and Omnius's replies to you". The first cut
    read that as "anything the bot posted", which in a desk channel means the
    permission asks, the 2FA prompts, the delegation mirrors and the deaf-desk
    alarms as well - the fleet's own nervous system, forwarded to a stranger's
    phone by default. Found by review before anyone was invited.

    So two conditions, and the message needs both: it must fall inside the
    REPLY WINDOW opened by that person's own message (closed by anyone else
    speaking, or by an hour passing), and it must not look like fleet business.
    The window is the real filter; the markers are the second net.
    """
    if not is_bot or relay_for:
        return False                           # a human, or another guest's words
    opened = float(cur.get(f"await:{key}") or 0)
    if not opened or time.time() - opened > REPLY_WINDOW_SECONDS:
        return False
    text = (m.get("content") or "")
    return not any(mark in text for mark in FLEET_MARKERS)


REPLY_WINDOW_SECONDS = 3600.0


def _send_to_telegram(token, chat_id, body, files):
    """One Discord message -> Telegram, split rather than truncated.

    Telegram's ceiling is 4096 characters and a desk's answer is regularly
    longer. api.send_message chunks in the other direction for the same reason:
    a cut-off answer looks like a broken agent, not a long one.
    """
    if files:
        tg(token, "sendDocument", {"chat_id": chat_id, "caption": body[:1000]},
           files=[("document", files[0])])
        body = body[1000:]
        if not body.strip():
            return
    pieces = [body[i:i + 3900] for i in range(0, len(body), 3900)] or [body]
    for piece in pieces[:6]:
        tg(token, "sendMessage", {"chat_id": chat_id, "text": piece})
    if len(pieces) > 6:
        tg(token, "sendMessage",
           {"chat_id": chat_id,
            "text": f"[... {len(pieces) - 6} more part(s) - see the channel]"})


def _fetch_for_telegram(api, msg):
    """Only when media_out is on. Oversize files are named, never uploaded."""
    out = []
    month = datetime.now().strftime("%Y-%m")
    for att in (msg.get("attachments") or [])[:1]:
        if int(att.get("size") or 0) > MAX_TG_UPLOAD:
            continue
        dest = ROOT / "media" / "sent" / month / f"tg-{msg['id']}-{sanitize(att.get('filename'))}"
        try:
            api.download(att["url"], dest)
            out.append(str(dest))
        except Exception as e:                                   # noqa: BLE001
            log(f"could not fetch {att.get('filename')}: {type(e).__name__}")
    return out


# --- main ----------------------------------------------------------------------

def describe():
    token, mirror, chats = load_config()
    print(f"token: {'set' if token else 'NOT SET'}   mirror every {mirror:.0f}s")
    if not chats:
        print("no [chat.*] blocks - nobody is invited (copy config\\telegram.example.ini)")
    for label, c in chats.items():
        print(f"  {label:14} tg {c['telegram_user_id']:14} -> #{c['discord_channel']}"
              f"  desk={c['desk']}  sees={c['visibility']}"
              f"  media_out={int(c['media_out'])}")
    for p in ocfg.problems():
        if "telegram" in p.lower():
            print(f"  [!] {p}")
    return 0


def main(argv=None):
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Telegram <-> one Discord channel.")
    ap.add_argument("--once", action="store_true", help="one pass, then exit")
    ap.add_argument("--check", action="store_true", help="report config and exit")
    a = ap.parse_args(argv)
    if a.check:
        return describe()

    import api                                  # after --check so a bad .env still reports
    me_id = ""
    log("telegram bridge starting")
    locked_for = None
    while True:
        token, mirror, chats = load_config()
        if token and locked_for != token:
            # Taken once the TOKEN is known, because the token is what the lock
            # is about. A bridge that never gets a token never competes for one.
            if not acquire_lock(token):
                # STANDBY, not exit. Exiting looked identical to crashing to the
                # watchdog, which then started a fresh doomed copy every minute
                # forever. Staying alive keeps its lease pid valid, costs an
                # idle process, and means we take over the moment the other
                # bridge stops.
                _beacon(0, "standby")
                if a.once:
                    return 0
                time.sleep(IDLE_SECONDS * 2)
                continue
            locked_for = token
        if not token or not chats:
            # Stamped while idle too, or a bridge waiting for a token would look
            # WEDGED to the watchdog's health check and be restarted every five
            # minutes forever. Alive-and-waiting is a state, not a fault.
            _beacon(0, "idle")
            if a.once:
                log("nothing configured - nothing to do")
                return 0
            # IDLE, never exit: the watchdog restarts a bridge it cannot find,
            # so a process that exits on purpose is indistinguishable from one
            # that crashed - and gets relaunched forever.
            time.sleep(IDLE_SECONDS)
            continue
        if not me_id:
            try:
                me_id = str((api.api("GET", "/users/@me") or {}).get("id") or "")
            except Exception as e:                               # noqa: BLE001
                log(f"cannot identify the bot yet: {type(e).__name__}: {e}")
                # Stamped here too: waiting for Discord (a revoked token, or a
                # laptop whose wifi is not up at logon) is a live process with a
                # real problem - not a wedged one to be killed every 5 minutes.
                _beacon(len(chats), "failing")
                if a.once:
                    return 1
                time.sleep(10)
                continue
        healthy = True
        try:
            inbound(token, chats, api)
        except TelegramError as e:
            log(f"inbound: {e}")
            healthy = False
            time.sleep(5)
        except Exception as e:                                   # noqa: BLE001
            log(f"inbound: {type(e).__name__}: {e}")
            healthy = False
            time.sleep(5)
        try:
            outbound(token, chats, api, me_id)
        except Exception as e:                                   # noqa: BLE001
            log(f"outbound: {type(e).__name__}: {e}")
            healthy = False
        _beacon(len(chats), "ok" if healthy else "failing")
        if a.once:
            return 0
        time.sleep(mirror)


def _beacon(n=None, state="ok"):
    """Proof of life, AND of what kind of life. -> nothing, never raises.

    Freshness alone was a lie in two directions, both found by review before
    anyone relied on them: a bridge with no token stamped nothing and looked
    wedged, and a bridge whose every Telegram call failed stamped a fresh
    timestamp and looked healthy. So the file says which:

      working - mid-pass, downloading or transcribing; alive, do not kill
      idle    - no token or no usable invite; nothing to do, nothing wrong
      ok      - last pass talked to Telegram without an error
      failing - last pass raised; the process lives, the service does not

    The watchdog reads the TIMESTAMP (is it stuck?); autostart reads the STATE
    (is it working?). Those are different questions and were being answered by
    one number.
    """
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        if n is None:                          # a mid-pass stamp knows the time,
            try:                               # not the roster - keep the roster
                n = json.loads((STATE / "beacon.json").read_text(
                    encoding="utf-8")).get("chats", 0)
            except (OSError, ValueError):
                n = 0
        (STATE / "beacon.json").write_text(json.dumps(
            {"at": utc_now(), "chats": n, "state": state}),
            encoding="utf-8")
    except OSError:
        pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except TelegramError as e:
        print(f"[X] {e}", file=sys.stderr)
        sys.exit(1)
