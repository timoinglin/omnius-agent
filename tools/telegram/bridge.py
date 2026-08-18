#!/usr/bin/env python3
"""Telegram <-> one Discord channel, for people who have no Discord account.

    python tools\\telegram\\bridge.py           run it (this is what the service runs)
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
from datetime import datetime
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
LOG = ROOT / "state" / "logs" / "telegram.log"


class TelegramError(Exception):
    """Something the owner should see. Mapped to exit 1 by main()."""


def log(msg):
    """One line, timestamped, token never included - see _safe()."""
    line = f"{datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ')} {_safe(str(msg))}"
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
    exiting, because its scheduled task carries a 1-minute self-heal trigger
    and a process that exits on purpose would be restarted forever - the exact
    boot-loop shape that cost 112 relaunches on 2026-08-18.
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
        line = {"ts": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
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


def inbound(token, chats, api):
    """Telegram -> Discord -> the desk's inbox. -> number of messages relayed."""
    cur = read_cursor("updates", {})
    offset = int(cur.get("offset") or 0)
    updates = tg(token, "getUpdates", {"offset": offset, "timeout": POLL_WAIT,
                                       "allowed_updates": json.dumps(["message"])},
                 timeout=POLL_WAIT + 15) or []
    relayed = 0
    for upd in updates:
        offset = max(offset, int(upd.get("update_id", 0)) + 1)
        msg = upd.get("message") or {}
        uid = str((msg.get("from") or {}).get("id") or "")
        chat_id = str((msg.get("chat") or {}).get("id") or "")
        who = next((lbl for lbl, c in chats.items()
                    if c["telegram_user_id"] == uid), None)
        if not who:
            # Silence is deliberate: an explanatory refusal would tell a stranger
            # that something is here. The id is logged so the owner can add it.
            log(f"ignored a message from telegram id {uid} - not in config\\telegram.ini")
            continue
        try:
            relayed += _relay_one(token, api, who, chats[who], msg, chat_id)
        except TelegramError as e:
            log(f"could not relay from {who}: {e}")
        except Exception as e:                                   # noqa: BLE001
            log(f"could not relay from {who}: {type(e).__name__}: {e}")
    write_cursor("updates", {"offset": offset})
    return relayed


def _relay_one(token, api, label, chat, msg, tg_chat_id):
    """One Telegram message -> a Discord post + an envelope for the desk."""
    text = (msg.get("text") or msg.get("caption") or "").strip()
    files, notes = [], []
    month = datetime.now().strftime("%Y-%m")
    mid = msg.get("message_id")

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
            spoken = transcribe(dest)
            if spoken:
                notes.append(f"🎙 {spoken}")
                text = (text + "\n" + spoken).strip() if text else spoken

    cid, cname, category = channel_of(api, chat)
    body = f"**{label}** (telegram): {text}" if text else f"**{label}** (telegram) sent a file"
    if notes and not text:
        body = f"**{label}** (telegram): " + " ".join(notes)
    posted = api.send_message(cid, body, files=[f["path"] for f in files] or None)
    discord_id = str((posted or [{}])[0].get("id") or "")
    if not discord_id:
        raise TelegramError("Discord accepted the post but returned no message id")

    # The envelope id IS the Discord message id: inbox names sort
    # lexicographically, so a prefixed stem would queue behind every snowflake -
    # and sharing the id makes the Discord message, the envelope, the transcript
    # and !trace one story.
    box = ROOT / "state" / "inbox" / chat["desk"]
    box.mkdir(parents=True, exist_ok=True)
    envelope = {"id": discord_id, "from": label, "channel": cname,
                "channelId": cid, "category": category,
                "ts": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "text": text, "files": files}
    # Atomic, like every other envelope writer: ensure_runners scans this folder
    # every 3 seconds and a half-written file would be read as a torn envelope.
    tmp = box / f"{discord_id}.json.tmp"
    tmp.write_text(json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(box / f"{discord_id}.json")
    transcribe_line(api, chat["desk"], "in", text, channel=cname, channel_id=cid,
                    files=[f["path"] for f in files], who=label)

    seen = read_cursor("posted", {"ids": []})
    seen["ids"] = (seen.get("ids") or [])[-500:] + [discord_id]
    write_cursor("posted", seen)
    log(f"{label} -> #{cname or cid} (desk {chat['desk']}, {len(files)} file(s))")
    return 1


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
    posted_ids = set((read_cursor("posted", {"ids": []}) or {}).get("ids") or [])
    sent = 0
    for label, chat in chats.items():
        try:
            cid, _cname, _cat = channel_of(api, chat)
        except Exception as e:                                   # noqa: BLE001
            log(f"{label}: channel {chat['discord_channel']} not found - {e}")
            continue
        last = cur.get(cid)
        try:
            if not last:
                # First run: start from NOW. Replaying a channel's history into
                # someone's phone the moment they are invited would be a
                # surprise, and under visibility=own it would be a leak.
                cur[cid] = str(api.latest_message_id(cid))
                continue
            msgs = sorted(api.messages_after(cid, last), key=lambda m: int(m["id"]))
        except Exception as e:                                   # noqa: BLE001
            log(f"mirror read failed for #{cid}: {type(e).__name__}: {e}")
            continue
        for m in msgs:
            mid = str(m["id"])
            if mid in posted_ids:
                cur[cid] = mid
                continue                       # our own relay coming back - never loop
            author = m.get("author") or {}
            is_bot = bool(author.get("bot")) or str(author.get("id")) == str(me_id)
            if chat["visibility"] == "own" and not is_bot:
                cur[cid] = mid
                continue                       # only their words and Omnius's replies
            name = "Omnius" if is_bot else (author.get("global_name")
                                            or author.get("username") or "owner")
            text = (m.get("content") or "").strip()
            body = f"{name}: {text}" if text else f"{name}:"
            body += describe_attachments(m)
            files = _fetch_for_telegram(api, m) if chat.get("media_out") else []
            try:
                _send_to_telegram(token, chat["telegram_user_id"], body, files)
                cur[cid] = mid                 # ONLY on success - see below
                cur.pop(f"stuck:{cid}", None)
                sent += 1
            except TelegramError as e:
                # The cursor does NOT advance on failure, so a Telegram outage
                # is retried rather than silently swallowing what the desk said.
                # But a permanent refusal (they blocked the bot) would then wedge
                # the mirror forever, so give up after three passes and SAY SO -
                # a dropped message that nobody is told about is the worst of
                # both designs.
                stuck = int(cur.get(f"stuck:{cid}") or 0) + 1
                cur[f"stuck:{cid}"] = stuck
                log(f"mirror to {label} failed ({stuck}/3): {e}")
                if stuck >= 3:
                    cur[cid] = mid
                    cur.pop(f"stuck:{cid}", None)
                    log(f"gave up mirroring message {mid} to {label} - skipped it")
                break                          # keep order: retry this one first
    write_cursor("mirror", cur)
    return sent


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
    while True:
        token, mirror, chats = load_config()
        if not token or not chats:
            if a.once:
                log("nothing configured - nothing to do")
                return 0
            # IDLE, never exit: the service task carries a 1-minute self-heal
            # trigger, so a process that exits on purpose is indistinguishable
            # from one that crashes - and gets relaunched forever.
            time.sleep(IDLE_SECONDS)
            continue
        if not me_id:
            try:
                me_id = str((api.api("GET", "/users/@me") or {}).get("id") or "")
            except Exception as e:                               # noqa: BLE001
                log(f"cannot identify the bot yet: {type(e).__name__}: {e}")
                time.sleep(10)
                continue
        try:
            inbound(token, chats, api)
        except TelegramError as e:
            log(f"inbound: {e}")
            time.sleep(5)
        except Exception as e:                                   # noqa: BLE001
            log(f"inbound: {type(e).__name__}: {e}")
            time.sleep(5)
        try:
            outbound(token, chats, api, me_id)
        except Exception as e:                                   # noqa: BLE001
            log(f"outbound: {type(e).__name__}: {e}")
        _beacon(len(chats))
        if a.once:
            return 0
        time.sleep(mirror)


def _beacon(n):
    """Proof of life for autostart's health probe, same idea as the watchdog's."""
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        (STATE / "beacon.json").write_text(json.dumps(
            {"at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), "chats": n}),
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
