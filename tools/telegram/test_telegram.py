"""Offline tests for tools\\telegram. No bot token, no network, no Discord.

Both directions are exercised against fakes, because the failures that matter
here are not crashes - they are a stranger being heard, the owner's private
traffic reaching an invited phone, or a desk's answer vanishing quietly. Each of
those looks like normal operation from the outside, so each gets a check.

    python tools\\telegram\\test_telegram.py
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "tools"))

import omnius_config as ocfg  # noqa: E402
import bridge  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_passed = _failed = 0


def check(label, cond, hint=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  [PASS] {label}")
    else:
        _failed += 1
        print(f"  [FAIL] {label}  {hint}")


SAND = Path(tempfile.mkdtemp(prefix="omnius-tgtest-"))
SRC = (HERE / "bridge.py").read_text(encoding="utf-8")


def _code_only(src):
    """The source with comments and docstrings removed.

    A check that greps the raw file cannot tell a rule from a sentence ABOUT the
    rule - and this file explains itself at length, so half of them would pass
    or fail on prose.
    """
    import io as _io
    import tokenize as _tok
    out = []
    for t in _tok.generate_tokens(_io.StringIO(src).readline):
        if t.type not in (_tok.COMMENT, _tok.STRING):
            out.append(t.string)
    return " ".join(out)


CODE = _code_only(SRC)

# Every path the bridge writes to is redirected BEFORE anything runs, so a test
# can never touch the live fleet - the same rule test_watchdog.py works under.
bridge.ROOT = SAND
bridge.STATE = SAND / "state" / "telegram"
bridge.TRANSCRIPTS = SAND / "state" / "transcripts"
bridge.MEDIA_IN = SAND / "media" / "inbox"
bridge.LOG = SAND / "state" / "logs" / "telegram.log"
_cfgdir = SAND / "config"
_cfgdir.mkdir(parents=True, exist_ok=True)
ocfg.CONFIG_DIR = _cfgdir

FAKE_TOKEN = "1234567890:AAF-fake-token-not-a-real-credential-xx"


def write_config(text):
    (_cfgdir / "telegram.ini").write_text(text, encoding="utf-8")


def inbox(desk):
    d = SAND / "state" / "inbox" / desk
    return sorted(p.name for p in d.glob("*.json")) if d.exists() else []


class FakeApi:
    """Just the four calls the bridge makes, plus a record of what it did."""

    def __init__(self, channels=None):
        self.posts = []
        self.downloads = []
        self.channels = channels or [
            {"id": "900000000001", "name": "general", "type": 0,
             "parent_id": "900000000000"},
            {"id": "900000000000", "name": "my-project", "type": 4},
            {"id": "900000000007", "name": "alerts", "type": 0},
        ]
        self.history = {}
        self._next = 700000000000

    def redact(self, t):
        return t

    def chunk_text(self, t, limit=1990):
        return [t[i:i + limit] for i in range(0, len(t), limit)] or [""]

    def resolve_channel(self, name_or_id, category=None):
        s = str(name_or_id).lstrip("#")
        for c in self.channels:
            if str(c["id"]) == s or c.get("name") == s:
                return c
        raise RuntimeError(f"channel not found: {name_or_id}")

    def guild_channels(self):
        return self.channels

    def send_message(self, cid, text, files=None):
        self._next += 1
        mid = str(self._next)
        self.posts.append({"channel": str(cid), "text": text,
                           "files": list(files or []), "id": mid})
        return [{"id": mid}]

    def latest_message_id(self, cid):
        return "600000000000"

    def messages_after(self, cid, after, limit=50):
        return [m for m in self.history.get(str(cid), []) if int(m["id"]) > int(after)]

    def download(self, url, dest):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"x")
        self.downloads.append(str(dest))
        return dest


_sent = []          # every outgoing Telegram call the bridge makes
_fail_next = [0]    # how many of the next sendMessage calls should fail


def fake_tg(token, method, params=None, files=None, timeout=None):
    if method == "sendMessage" and _fail_next[0] > 0:
        _fail_next[0] -= 1
        raise bridge.TelegramError("Forbidden: bot was blocked by the user")
    _sent.append({"method": method, "params": params or {}, "files": files or []})
    return {"ok": True}


def fake_download(token, file_id, dest):
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    Path(dest).write_bytes(b"audio-bytes")
    return dest


bridge.tg = fake_tg
bridge.tg_download = fake_download
bridge.transcribe = lambda p: "seven plus seven"      # whisper is tested elsewhere


def msg(uid, text="hello", mid=42, **extra):
    m = {"message_id": mid, "from": {"id": uid}, "chat": {"id": uid}, "text": text}
    m.update(extra)
    return {"update_id": mid, "message": m}


CHATS = {"antonio": {"telegram_user_id": "555111", "discord_channel": "general",
                     "desk": "my-project.web", "visibility": "own",
                     "media_out": False, "name": "antonio"}}


# --- the allow-list is the whole security model --------------------------------
print("== who is heard ==")
write_config("[telegram]\ntoken_env = TELEGRAM_BOT_TOKEN\n")
check("no [chat.*] block means nobody is invited", ocfg.telegram_chats() == {})

for bad, why in [
        ("[chat.x]\ntelegram_user_id = not-a-number\ndiscord_channel = g\ndesk = d\n",
         "a half-pasted id"),
        ("[chat.x]\ntelegram_user_id = 5\ndesk = d\n", "no channel"),
        ("[chat.x]\ntelegram_user_id = 5\ndiscord_channel = g\ndesk = d\n"
         "visibility = everything\n", "an invented visibility"),
        ("[chat.orchestrator]\ntelegram_user_id = 5\ndiscord_channel = g\ndesk = d\n",
         "a label that impersonates the orchestrator"),
        ("[chat.my-project.web]\ntelegram_user_id = 5\ndiscord_channel = g\ndesk = d\n",
         "a label shaped like a desk id"),
        ("[chat.x-job]\ntelegram_user_id = 5\ndiscord_channel = g\ndesk = d\n",
         "a label shaped like a job sender")]:
    write_config("[telegram]\n" + bad)
    check(f"{why} admits nobody", ocfg.telegram_chats() == {},
          f"got {ocfg.telegram_chats()}")

write_config("[telegram]\n[chat.antonio]\ntelegram_user_id = 555111\n"
             "discord_channel = general\ndesk = my-project.web\n")
_c = ocfg.telegram_chats()
check("a complete block is accepted", list(_c) == ["antonio"])
check("visibility defaults to own, not all - his other messages stay his",
      _c["antonio"]["visibility"] == "own")
check("media_out defaults to off", _c["antonio"]["media_out"] is False)

# The channel already knows its desk - asking for it twice only created a way to
# disagree with the fleet. Omitted here, resolved at relay time from the same
# map every message the owner types goes through.
write_config("[telegram]\n[chat.antonio]\ntelegram_user_id = 555111\n"
             "discord_channel = general\n")
check("desk is OPTIONAL - a channel without one is still a valid invite",
      list(ocfg.telegram_chats()) == ["antonio"]
      and ocfg.telegram_chats()["antonio"]["desk"] == "")
check("the bridge resolves it from the fleet's own map, not a second copy",
      "build_map" in CODE and "def desk_for_channel" in CODE,
      "two copies of a channel->desk mapping is how they drift apart")
check("every rejection above was reported, not swallowed",
      any("telegram" in p.lower() for p in ocfg.problems()))

# LIVE, 2026-08-19: one leading space made INI read `media_out = 0` as part of
# the value above it, so `visibility` became "own\nmedia_out = 0", the entry was
# refused, and the bridge sat idle with nothing obviously wrong in the file.
write_config("[telegram]\n[chat.antonio]\ntelegram_username = antonio_h\n"
             "discord_channel = general\nvisibility = own\n media_out = 1\n")
_indented = ocfg.telegram_chats()
check("an indented setting is put back where it belongs, not swallowed",
      list(_indented) == ["antonio"]
      and _indented["antonio"]["visibility"] == "own"
      and _indented["antonio"]["media_out"] is True,
      f"got {_indented}")
check("...and the stray space is named, so it gets fixed rather than lived with",
      any("was indented" in p for p in ocfg.problems()))

# One person, one channel. Listed twice, the bridge would have matched whichever
# block sorted first and dropped the other without a word - so they would be
# writing into a channel nobody told them about.
write_config("[telegram]\n"
             "[chat.antonio]\ntelegram_user_id = 555111\n"
             "discord_channel = general\ndesk = my-project.web\n"
             "[chat.antonio-two]\ntelegram_user_id = 555111\n"
             "discord_channel = other\ndesk = my-project.api\n")
_dup = ocfg.telegram_chats()
check("the same telegram id in two blocks keeps only the first, and SAYS so",
      list(_dup) == ["antonio"]
      and any("names the same person" in p for p in ocfg.problems()),
      f"got {list(_dup)}")

write_config("[telegram]\n[chat.antonio]\ntelegram_user_id = 555111\n"
             "discord_channel = general\ndesk = no-such-projekt.web\n")
_typo = ocfg.telegram_chats()
check("a desk with no folder is REPORTED but still let in - the failed run is "
      "louder than a silent drop, and the project may not exist yet",
      list(_typo) == ["antonio"]
      and any("has no folder" in p for p in ocfg.problems()))

write_config("[telegram]\n"
             "[chat.antonio]\ntelegram_user_id = 555111\n"
             "discord_channel = general\ndesk = my-project.web\n"
             "[chat.berta]\ntelegram_user_id = 555222\n"
             "discord_channel = other-channel\ndesk = other-project.web\n")
_two = ocfg.telegram_chats()
# visibility=own is a privacy PROMISE, and in a shared room it cannot be kept:
# the desk's answer to one guest is an ordinary bot message to the other.
write_config("[telegram]\n"
             "[chat.antonio]\ntelegram_user_id = 555111\n"
             "discord_channel = general\nvisibility = own\n"
             "[chat.berta]\ntelegram_user_id = 555222\n"
             "discord_channel = general\nvisibility = all\n")
_shared = ocfg.telegram_chats()
check("'own' in a channel someone else shares is refused, not quietly broken",
      list(_shared) == ["berta"]
      and any("cannot be honoured" in p for p in ocfg.problems()),
      f"got {list(_shared)}")
write_config("[telegram]\n"
             "[chat.antonio]\ntelegram_user_id = 555111\n"
             "discord_channel = general\nvisibility = all\n"
             "[chat.berta]\ntelegram_user_id = 555222\n"
             "discord_channel = general\nvisibility = all\n")
check("...but a shared room where everyone sees everything is fine",
      len(ocfg.telegram_chats()) == 2)

write_config("[telegram]\n"
             "[chat.antonio]\ntelegram_user_id = 555111\n"
             "discord_channel = general\ndesk = my-project.web\n"
             "[chat.berta]\ntelegram_user_id = 555222\n"
             "discord_channel = other-channel\ndesk = other-project.web\n")
_two = ocfg.telegram_chats()
check("two people in DIFFERENT channels both keep visibility=own",
      len(_two) == 2 and all(b["visibility"] == "own" for b in _two.values()))
check("two people, two channels, two desks - one bot serves them all",
      len(_two) == 2
      and _two["antonio"]["discord_channel"] != _two["berta"]["discord_channel"]
      and _two["antonio"]["desk"] != _two["berta"]["desk"])

# --- telegram -> discord -------------------------------------------------------
print("\n== inbound ==")
api = FakeApi()
bridge._CHANNELS.clear()
bridge.tg = lambda token, method, params=None, files=None, timeout=None: (
    [msg(999999, "let me in")] if method == "getUpdates" else {"ok": True})
n = bridge.inbound(FAKE_TOKEN, CHATS, api)
_to_channel = [p for p in api.posts if p["channel"] != "900000000007"]
check("an unlisted telegram id relays nothing into a guest channel",
      n == 0 and not _to_channel)
check("...but the OWNER is told in Discord, with the block to paste",
      any("not invited" in p["text"] and "555111" not in p["text"]
          for p in api.posts if p["channel"] == "900000000007"),
      "digging a numeric id out of state\logs\ is not something to ask of "
      "someone running his fleet from a phone")
check("...and their message is NOT quoted - it is untrusted text",
      not any("let me in" in p["text"] for p in api.posts))
api.posts.clear()
check("...and writes no envelope, so no desk is even started",
      inbox("my-project.web") == [])
check("...and is answered with silence, not an explanation",
      not [s for s in _sent if s["method"] == "sendMessage"])
check("...but its id is logged, which is how you invite someone",
      "999999" in bridge.LOG.read_text(encoding="utf-8"))

# A bot cannot write to someone who has not opened the chat, so /start is the
# first thing that ever arrives - from a stranger AND from an invited person.
_sent.clear()
bridge.tg = fake_tg
_start = [msg(555111, "/start", mid=3)]
bridge.tg = lambda token, method, params=None, files=None, timeout=None: (
    _start if method == "getUpdates" else fake_tg(token, method, params, files, timeout))
_before_start = inbox("my-project.web")
bridge.inbound(FAKE_TOKEN, CHATS, api)
check("/start is answered in Telegram, not relayed into the channel",
      not api.posts and inbox("my-project.web") == _before_start
      and any("Connected" in s["params"].get("text", "") for s in _sent),
      "relaying it would spend a desk run on the word /start")

bridge.tg = fake_tg
api = FakeApi()
bridge._CHANNELS.clear()
bridge.tg = lambda token, method, params=None, files=None, timeout=None: (
    [msg(555111, "hola Omnius", mid=7)] if method == "getUpdates" else {"ok": True})
n = bridge.inbound(FAKE_TOKEN, CHATS, api)
check("a listed id is relayed", n == 1 and len(api.posts) == 1)
check("the post says who wrote it", "**antonio** (telegram)" in api.posts[0]["text"])
_env_names = inbox("my-project.web")
check("exactly one envelope reached the desk", len(_env_names) == 1)
_env = json.loads((SAND / "state" / "inbox" / "my-project.web" /
                   _env_names[0]).read_text(encoding="utf-8"))
check("the envelope carries the guest label, NEVER owner", _env["from"] == "antonio",
      f"got {_env['from']!r} - a guest wearing owner would unlock every control verb")
check("the envelope id IS the discord message id, so the inbox sorts honestly",
      _env["id"] == api.posts[0]["id"] and _env_names[0] == api.posts[0]["id"] + ".json")
check("an older discord snowflake still sorts first next to it",
      sorted(["600000000000.json", _env_names[0]])[0] == "600000000000.json")
check("channel and category are resolved, so the desk knows where it is",
      _env["channel"] == "general" and _env["category"] == "my-project")
check("channelId is the id, not the name", _env["channelId"] == "900000000001")
check("nothing was written to state\\outbox - the watchdog owns replies",
      not (SAND / "state" / "outbox").exists())
check("the exchange is in the desk transcript, so !trace can find it",
      (SAND / "state" / "transcripts" / "my-project.web").exists())

# media + voice
bridge.tg = lambda token, method, params=None, files=None, timeout=None: (
    [msg(555111, "", mid=8, voice={"file_id": "v1"})]
    if method == "getUpdates" else {"ok": True})
bridge.inbound(FAKE_TOKEN, CHATS, api)
_env2 = json.loads(sorted((SAND / "state" / "inbox" / "my-project.web").glob("*.json"),
                          key=lambda p: p.name)[-1].read_text(encoding="utf-8"))
check("a voice note is downloaded into the media archive",
      _env2["files"] and "media" in _env2["files"][0]["path"]
      and "inbox" in _env2["files"][0]["path"])
check("...and transcribed, so the desk gets words not a blob",
      "seven plus seven" in _env2["text"])
check("...and the channel shows the transcription too",
      "seven plus seven" in api.posts[-1]["text"])

# --- inviting by @handle -------------------------------------------------------
# Nobody knows their own Telegram user id. Requiring one meant the owner had to
# read a log file before he could invite anybody, which is not something to ask
# of someone running his fleet from a phone.
print("\n== invited by @handle ==")
write_config("[telegram]\n[chat.antonio]\ntelegram_username = @Antonio\n"
             "discord_channel = general\n")
_h = ocfg.telegram_chats()
check("an @handle is a valid invite on its own",
      list(_h) == ["antonio"] and _h["antonio"]["telegram_username"] == "antonio"
      and _h["antonio"]["telegram_user_id"] == "",
      f"got {_h}")
for bad in ("[chat.x]\ntelegram_username = not a handle!\ndiscord_channel = g\n",
            "[chat.x]\ndiscord_channel = g\n"):
    write_config("[telegram]\n" + bad)
    check("a malformed handle (or none at all) admits nobody",
          ocfg.telegram_chats() == {}, f"got {ocfg.telegram_chats()}")

api = FakeApi()
api._next = 830000000000
bridge._CHANNELS.clear(); bridge._DESKS.clear()
shutil.rmtree(bridge.STATE, ignore_errors=True)
_byname = {"antonio": {"telegram_user_id": "", "telegram_username": "antonio",
                       "discord_channel": "general", "desk": "my-project.web",
                       "visibility": "own", "media_out": False, "name": "antonio"}}
_m = msg(555111, "hola", mid=41)
_m["message"]["from"]["username"] = "Antonio"
bridge.tg = lambda token, method, params=None, files=None, timeout=None: (
    [_m] if method == "getUpdates" else fake_tg(token, method, params, files, timeout))
bridge.inbound(FAKE_TOKEN, _byname, api)
check("a message from that handle is relayed",
      any("hola" in p["text"] for p in api.posts))
check("...and the numeric id behind it is PINNED",
      (bridge.read_cursor("pinned", {}) or {}).get("antonio") == "555111",
      "a handle can be released and re-registered by a stranger; the id cannot")

# Someone else takes the handle later. The invite must not follow it.
_impostor = msg(999000, "soy antonio", mid=42)
_impostor["message"]["from"]["username"] = "Antonio"
api.posts.clear()
bridge.tg = lambda token, method, params=None, files=None, timeout=None: (
    [_impostor] if method == "getUpdates" else fake_tg(token, method, params, files, timeout))
bridge.inbound(FAKE_TOKEN, _byname, api)
check("a DIFFERENT id claiming the same handle is not let in",
      not any("soy antonio" in p["text"] for p in api.posts),
      "the pin is what makes inviting by handle safe")
bridge.tg = fake_tg

# LIVE FINDING, 2026-08-19: the id was pinned and the message relayed, but the
# desk's answer went to an empty chat_id (HTTP 400) - the mirror read the id
# from the config, which an invite-by-handle leaves blank. Two review rounds
# missed it because every fixture carried an id.
check("an invite by handle sends to the PINNED id, not the empty config field",
      bridge.telegram_id_of("antonio", _byname["antonio"]) == "555111")
check("...and an id in the config still wins",
      bridge.telegram_id_of("antonio", CHATS["antonio"]) == "555111")
check("...while someone who has never written has nowhere to send, and says so "
      "by returning nothing rather than an empty API call",
      bridge.telegram_id_of("nobody", {"telegram_user_id": "",
                                       "telegram_username": "nobody"}) == "")
api.history["900000000001"] = [
    {"id": "600000000021", "content": "answer for a handle invite",
     "author": {"id": "222", "username": "omnius", "bot": True}, "attachments": []},
]
bridge.write_cursor("mirror", {"antonio@900000000001": "600000000020"})
bridge.open_reply_window("antonio", "900000000001")
_sent.clear()
bridge.outbound(FAKE_TOKEN, _byname, api, "222")
check("so a handle-invited person actually receives the desk's answer",
      any(s["params"].get("chat_id") == "555111" for s in _sent),
      "this is the whole point of the feature and it was broken end to end")

# desk resolved from the channel, exactly as the watchdog would
print("\n== the channel already knows its desk ==")
import types  # noqa: E402
_fake_wd = types.ModuleType("watchdog")


class _Target:
    def __init__(self, session):
        self.session = session


_fake_wd.build_map = lambda schema: {"900000000001": _Target("my-project.web"),
                                     "900000000009": _Target(None)}
sys.modules["watchdog"] = _fake_wd
FakeApi.load_schema = lambda self: {}

api = FakeApi()
api._next = 800000000000     # a fresh fake restarts its ids; keep them unique
bridge._CHANNELS.clear()
bridge._DESKS.clear()
_no_desk = {"antonio": dict(CHATS["antonio"], desk="")}
bridge.tg = lambda token, method, params=None, files=None, timeout=None: (
    [msg(555111, "sin desk", mid=11)] if method == "getUpdates" else {"ok": True})
_before = set(inbox("my-project.web"))
bridge.inbound(FAKE_TOKEN, _no_desk, api)
check("with no desk configured, the channel's own desk gets the envelope",
      len(set(inbox("my-project.web")) - _before) == 1,
      "the fleet already maps #web in my-project to my-project.web")

api.channels.append({"id": "900000000009", "name": "alerts", "type": 0})
bridge._CHANNELS.clear(); bridge._DESKS.clear()
_orphan = {"antonio": dict(CHATS["antonio"], desk="", discord_channel="alerts")}
_posts_before = len(api.posts)
bridge.tg = lambda token, method, params=None, files=None, timeout=None: (
    [msg(555111, "hola?", mid=12)] if method == "getUpdates" else {"ok": True})
bridge.inbound(FAKE_TOKEN, _orphan, api)
check("a channel that answers to NO desk relays nothing at all",
      len(api.posts) == _posts_before,
      "posting it would look handled while nothing was ever going to answer")
check("...and says why, naming the fix", "answers to no desk" in
      bridge.LOG.read_text(encoding="utf-8"))
bridge.tg = fake_tg

# --- discord -> telegram -------------------------------------------------------
print("\n== outbound ==")
_sent.clear()
bridge.tg = fake_tg
api = FakeApi()
bridge._CHANNELS.clear()
shutil.rmtree(bridge.STATE, ignore_errors=True)
bridge.outbound(FAKE_TOKEN, CHATS, api, "111")
check("the first pass starts from now - no history is replayed into their phone",
      _sent == [],
      "being invited must not dump the last month of a channel to a stranger")
_cursor = json.loads((bridge.STATE / "mirror.json").read_text(encoding="utf-8"))
check("the bridge keeps its OWN cursor file, keyed per person",
      _cursor.get("antonio@900000000001"),
      f"got {_cursor}")
bridge.write_cursor("mirror", {"900000000001": "600000000042"})
bridge.outbound(FAKE_TOKEN, CHATS, api, "222")
_migrated = json.loads((bridge.STATE / "mirror.json").read_text(encoding="utf-8"))
check("a cursor written by the previous version is migrated, not abandoned",
      _migrated.get("antonio@900000000001") == "600000000042"
      and "900000000001" not in _migrated,
      "an unmigrated key looks like a first run and jumps to NOW, swallowing "
      "whatever the desk said while the update ran")
bridge.write_cursor("mirror", {"antonio@900000000001": "600000000000"})
check("it never touches the watchdog's last_ids.json",
      "last_ids" not in CODE and not (SAND / "state" / "watchdog").exists(),
      "two cursors over one channel is fine; SHARING one is how the watchdog "
      "starts skipping the owner's messages")

# A realistic channel: the owner says something, the guest writes (relayed by us,
# so bot-authored and recorded), then the desk answers the guest.
api.history["900000000001"] = [
    {"id": "600000000001", "content": "owner private note",
     "author": {"id": "111", "username": "the-owner", "bot": False}, "attachments": []},
    {"id": "600000000002", "content": "**antonio** (telegram): where is the report?",
     "author": {"id": "222", "username": "omnius", "bot": True}, "attachments": []},
    {"id": "600000000003", "content": "here you go",
     "author": {"id": "222", "username": "omnius", "bot": True},
     "attachments": [{"filename": "report.pdf", "size": 10, "url": "http://x/y"}]},
]
bridge.write_cursor("posted", {"by": {"600000000002": "antonio"}})
_sent.clear()
# They just wrote, so the desk's next words are for them: that is what opens the
# reply window. Without it, `own` would forward every bot post in the channel.
bridge.open_reply_window("antonio", "900000000001")
bridge.outbound(FAKE_TOKEN, CHATS, api, "222")
_texts = [s["params"].get("text", "") for s in _sent]
check("visibility=own hides the owner's own messages",
      not any("owner private note" in t for t in _texts),
      f"leaked: {_texts}")
check("visibility=own still delivers Omnius's reply",
      any("here you go" in t for t in _texts))
check("the reply says who is speaking", any(t.startswith("Omnius:") for t in _texts))
# THE round-2 finding: `own` used to mean "anything the bot posted", and in a
# desk channel that is the fleet's own nervous system - permission asks, 2FA
# prompts, delegation mirrors - forwarded to a guest's phone by default.
_fleet = [
    {"id": "600000000004", "content": "🔐 `ionos` wants a 6-digit code — reply with it",
     "author": {"id": "222", "username": "omnius", "bot": True}, "attachments": []},
    {"id": "600000000005", "content": "[desk mail] my-project.web -> my-project.api",
     "author": {"id": "222", "username": "omnius", "bot": True}, "attachments": []},
]
api.history["900000000001"] = api.history["900000000001"] + _fleet
bridge.write_cursor("mirror", {"antonio@900000000001": "600000000003"})
bridge.open_reply_window("antonio", "900000000001")
_sent.clear()
bridge.outbound(FAKE_TOKEN, CHATS, api, "222")
_leaked = " ".join(s["params"].get("text", "") for s in _sent)
check("a 2FA prompt is never mirrored to an invited person",
      "6-digit" not in _leaked, f"leaked: {_leaked}")
check("nor is the fleet talking to itself", "[desk mail]" not in _leaked)

# ...and the window closes the moment the owner speaks: what the desk says next
# is answering HIM.
api.history["900000000001"] = [
    {"id": "600000000008", "content": "check the invoice totals",
     "author": {"id": "111", "username": "the-owner", "bot": False}, "attachments": []},
    {"id": "600000000009", "content": "they are off by 40 EUR in March",
     "author": {"id": "222", "username": "omnius", "bot": True}, "attachments": []},
]
bridge.write_cursor("mirror", {"antonio@900000000001": "600000000007"})
bridge.open_reply_window("antonio", "900000000001")
_sent.clear()
bridge.outbound(FAKE_TOKEN, CHATS, api, "222")
check("an answer to the OWNER does not reach the guest either",
      not any("40 EUR" in s["params"].get("text", "") for s in _sent),
      "own means their conversation, not every word the desk says in that room")
api.history["900000000001"] = [
    {"id": "600000000001", "content": "owner private note",
     "author": {"id": "111", "username": "the-owner", "bot": False}, "attachments": []},
    {"id": "600000000002", "content": "**antonio** (telegram): where is the report?",
     "author": {"id": "222", "username": "omnius", "bot": True}, "attachments": []},
    {"id": "600000000003", "content": "here you go",
     "author": {"id": "222", "username": "omnius", "bot": True},
     "attachments": [{"filename": "report.pdf", "size": 10, "url": "http://x/y"}]},
]
bridge.write_cursor("posted", {"by": {"600000000002": "antonio"}})
bridge.write_cursor("mirror", {"antonio@900000000001": "600000000001"})
_sent.clear()
bridge.outbound(FAKE_TOKEN, CHATS, api, "222")
_texts = [s["params"].get("text", "") for s in _sent]
check("media_out=0 names attachments instead of uploading them",
      any("report.pdf" in t for t in _texts)
      and not [s for s in _sent if s["method"] == "sendDocument"]
      and not api.downloads)

_all = {"antonio": dict(CHATS["antonio"], visibility="all")}
shutil.rmtree(bridge.STATE, ignore_errors=True)
bridge.outbound(FAKE_TOKEN, _all, api, "222")          # first pass = cursor only
bridge.write_cursor("mirror", {"antonio@900000000001": "600000000000"})
_sent.clear()
bridge.outbound(FAKE_TOKEN, _all, api, "222")
check("visibility=all does deliver the owner's messages",
      any("owner private note" in s["params"].get("text", "") for s in _sent))

# loop guard
bridge.write_cursor("mirror", {"antonio@900000000001": "600000000000"})
bridge.write_cursor("posted", {"by": {"600000000003": "antonio"}})
_sent.clear()
bridge.outbound(FAKE_TOKEN, _all, api, "222")
check("a message the bridge relayed for THEM is never mirrored back to them",
      not any("here you go" in s["params"].get("text", "") for s in _sent),
      "without this every relayed message echoes forever")

# two people, one channel
print("\n== a shared room ==")
api.history["900000000001"] = [
    {"id": "600000000006", "content": "**berta** (telegram): buenas",
     "author": {"id": "222", "username": "omnius", "bot": True}, "attachments": []},
]
_room = {"antonio": dict(CHATS["antonio"], visibility="all"),
         "berta": dict(CHATS["antonio"], telegram_user_id="555222",
                       visibility="all", name="berta")}
bridge.write_cursor("posted", {"by": {"600000000006": "berta"}})
bridge.write_cursor("mirror", {"antonio@900000000001": "600000000005",
                               "berta@900000000001": "600000000005"})
_sent.clear()
bridge.outbound(FAKE_TOKEN, _room, api, "222")
_to_antonio = [s["params"]["text"] for s in _sent
               if s["params"].get("chat_id") == "555111"]
_to_berta = [s["params"]["text"] for s in _sent
             if s["params"].get("chat_id") == "555222"]
check("under visibility=all the other guest's message DOES arrive",
      any("buenas" in t for t in _to_antonio),
      "a shared room where nobody hears anybody is not a room")
check("...attributed to them, not to Omnius which posted it",
      any(t.startswith("berta (telegram):") for t in _to_antonio), f"got {_to_antonio}")
check("...with our own Discord markup stripped, not echoed back at them",
      not any("**berta**" in t for t in _to_antonio))
check("berta does not receive her own message back",
      not any("buenas" in t for t in _to_berta))

# THE bug an adversarial review caught (2026-08-18), which the first version of
# this very test had blessed: one cursor per CHANNEL, advanced inside a loop over
# PEOPLE, meant the first label consumed the room and everyone else in it went
# permanently deaf - including to answers to their own questions.
api.history["900000000001"] = [
    {"id": "600000000007", "content": "the report is ready",
     "author": {"id": "222", "username": "omnius", "bot": True}, "attachments": []},
]
bridge.write_cursor("posted", {"by": {}})
bridge.write_cursor("mirror", {"antonio@900000000001": "600000000006",
                               "berta@900000000001": "600000000006"})
_sent.clear()
bridge.outbound(FAKE_TOKEN, _room, api, "222")
check("in a shared room BOTH people get the desk's answer, not just the first",
      any("the report is ready" in s["params"]["text"]
          for s in _sent if s["params"].get("chat_id") == "555111")
      and any("the report is ready" in s["params"]["text"]
              for s in _sent if s["params"].get("chat_id") == "555222"),
      "a cursor keyed by channel alone made the second person permanently deaf")

bridge.write_cursor("mirror", {"antonio@900000000001": "600000000005"})
_own = {"antonio": dict(CHATS["antonio"], visibility="own")}
_sent.clear()
bridge.outbound(FAKE_TOKEN, _own, api, "222")
check("under visibility=own another guest's message stays hidden",
      not any("buenas" in s["params"].get("text", "") for s in _sent),
      "own means their words and Omnius's replies - not the room")
api.history["900000000001"] = api.history["900000000001"][:0] + [
    {"id": "600000000001", "content": "owner private note",
     "author": {"id": "111", "username": "the-owner", "bot": False}, "attachments": []},
    {"id": "600000000002", "content": "here you go",
     "author": {"id": "222", "username": "omnius", "bot": True},
     "attachments": [{"filename": "report.pdf", "size": 10, "url": "http://x/y"}]},
]

# one bridge at a time
print("\n== single instance ==")
bridge.write_cursor("posted", {"by": {}})
(bridge.STATE / "lock.json").write_text(
    json.dumps({"pid": 999999999, "startedAt": "2026-01-01T00:00:00Z"}), encoding="utf-8")
check("a lock held by a DEAD pid is stale, not a wall", bridge.acquire_lock() is True)
_real_alive = bridge.pid_alive
bridge.pid_alive = lambda pid, expect="python": True
import time as _t  # noqa: E402
(bridge.STATE / "lock.json").write_text(
    json.dumps({"pid": 424242, "startedAt": "x", "startedTs": _t.time()}),
    encoding="utf-8")
check("a lock held by a LIVE bridge stops the second one",
      bridge.acquire_lock() is False,
      "telegram allows one getUpdates consumer per bot; two pollers split the mail")
(bridge.STATE / "lock.json").write_text(
    json.dumps({"pid": 424242, "startedAt": "x", "startedTs": _t.time() - 99999}),
    encoding="utf-8")
if (bridge.STATE / "beacon.json").exists():          # nothing stamped since
    import os as _os  # noqa: E402
    _os.utime(bridge.STATE / "beacon.json", (_t.time() - 200000, _t.time() - 200000))
check("an OLD lock whose pid never stamped a beacon is stolen, not obeyed",
      bridge.acquire_lock() is True,
      "a pid recycled across a reboot must not lock telegram out of the machine")
bridge.pid_alive = _real_alive
check("identity is checked, not just the number - windows reuses pids",
      "expect" in CODE and "_process_image" in CODE)

# failure handling
bridge.write_cursor("mirror", {"antonio@900000000001": "600000000001"})
bridge.write_cursor("posted", {"by": {}})
_sent.clear()
_fail_next[0] = 1
bridge.outbound(FAKE_TOKEN, _all, api, "222")
_cur = json.loads((bridge.STATE / "mirror.json").read_text(encoding="utf-8"))
check("a failed mirror does NOT advance the cursor - it is retried, not lost",
      _cur["antonio@900000000001"] == "600000000001")
_fail_next[0] = 5
for _ in range(3):
    bridge.outbound(FAKE_TOKEN, _all, api, "222")
_cur = json.loads((bridge.STATE / "mirror.json").read_text(encoding="utf-8"))
check("after three failures it moves on rather than wedging the mirror",
      _cur["antonio@900000000001"] == "600000000002")
check("...and says so, so a dropped message is never silent",
      "gave up mirroring" in bridge.LOG.read_text(encoding="utf-8"))
_fail_next[0] = 0

# a long answer is split, not cut
_sent.clear()
bridge._send_to_telegram(FAKE_TOKEN, "555111", "x" * 9000, [])
check("a long desk answer is split across messages, not truncated",
      len(_sent) >= 3 and sum(len(s["params"]["text"]) for s in _sent) >= 9000)

# --- what the beacon claims ----------------------------------------------------
# Freshness alone lied in BOTH directions: a bridge with no token stamped
# nothing and looked wedged; a bridge whose every call failed stamped a fresh
# timestamp and looked healthy. The watchdog reads the time, autostart the state.
print("\n== the beacon says which kind of alive ==")


def _beacon_state():
    return json.loads((bridge.STATE / "beacon.json").read_text(encoding="utf-8"))


bridge._beacon(0, "idle")
check("an unconfigured bridge still stamps, so it is not killed as wedged",
      _beacon_state()["state"] == "idle" and _beacon_state()["at"])
bridge._beacon(2, "ok")
check("a good pass says ok", _beacon_state()["state"] == "ok")
bridge._beacon(None, "working")
check("a mid-pass stamp keeps the roster it does not know",
      _beacon_state()["state"] == "working" and _beacon_state()["chats"] == 2,
      "whisper can hold one pass for ten minutes; the stamp must not go stale")
bridge._beacon(2, "failing")
check("a pass that raised says failing, not ok",
      _beacon_state()["state"] == "failing",
      "a revoked token leaves a happy-looking process that relays nothing")
check("the source stamps before the slow part, not only after it",
      CODE.count("_beacon") >= 5 and "working" in SRC)

# a long Telegram message becomes several Discord messages
print("\n== chunked posts ==")
api = FakeApi()
api._next = 810000000000
bridge._CHANNELS.clear(); bridge._DESKS.clear()
_orig_send = FakeApi.send_message
bridge.tg = lambda token, method, params=None, files=None, timeout=None: (
    [msg(555111, "x" * 2500, mid=21)] if method == "getUpdates" else {"ok": True})
bridge.write_cursor("posted", {"by": {}})
bridge.inbound(FAKE_TOKEN, CHATS, api)
_by = (bridge.read_cursor("posted", {"by": {}}) or {}).get("by") or {}
check("EVERY chunk of a long message is remembered, not just the first",
      len(_by) == 2 and len(api.posts) == 2,
      f"the forgotten tail was mirrored back to its own author as Omnius; got {_by}")
bridge.tg = fake_tg

# a relay that fails must not confirm the update to Telegram
print("\n== a message that could not be delivered ==")
api = FakeApi()
api._next = 820000000000
bridge._CHANNELS.clear(); bridge._DESKS.clear()
shutil.rmtree(bridge.STATE, ignore_errors=True)


def _boom(self, cid, text, files=None):
    raise RuntimeError("discord 503")


FakeApi.send_message = _boom
bridge.tg = lambda token, method, params=None, files=None, timeout=None: (
    [msg(555111, "call me back", mid=31)] if method == "getUpdates" else {"ok": True})
bridge.inbound(FAKE_TOKEN, CHATS, api)
_upd = bridge.read_cursor("updates", {})
check("a failed relay does NOT confirm the update - telegram will resend it",
      _upd.get("offset", 0) <= 31 and _upd.get("misses", {}).get("31") == 1,
      "a confirmed offset is delivered forever; the guest saw 'sent' and got silence")
for _ in range(2):
    bridge.inbound(FAKE_TOKEN, CHATS, api)
_upd = bridge.read_cursor("updates", {})
check("...but it gives up after three tries rather than blocking the queue",
      _upd.get("offset") == 32 and not _upd.get("misses"))
check("...and says that nothing of it reached Discord",
      "giving up on that message" in bridge.LOG.read_text(encoding="utf-8"))
FakeApi.send_message = _orig_send
bridge.tg = fake_tg

# --- secrets and posture -------------------------------------------------------
print("\n== posture ==")
bridge.log(f"pretend leak {FAKE_TOKEN} in a message")
_log = bridge.LOG.read_text(encoding="utf-8")
check("a bot token never survives into the log", FAKE_TOKEN not in _log
      and "<TELEGRAM_TOKEN>" in _log)
check("the token is read from .env by KEY, never stored in config",
      "env_value" in SRC and "token_env" in SRC)
check("config\\telegram.example.ini contains no real-looking id",
      not __import__("re").search(r"\b\d{15,22}\b",
                                  (ROOT / "config" / "telegram.example.ini")
                                  .read_text(encoding="utf-8")))
check("unconfigured means idle, never exit - a task with a 1-minute self-heal "
      "trigger would relaunch it forever",
      "time.sleep(IDLE_SECONDS)" in SRC and "IDLE_SECONDS" in SRC)
check("stdlib only - it must work on a fresh install with no pip step",
      "import requests" not in SRC and "import httpx" not in SRC
      and "urllib.request" in SRC)
check("this is a library, not a desk: no settings.json, so no channel is stamped",
      not (HERE / ".claude" / "settings.json").exists())
_wd = (ROOT / "tools" / "discord" / "watchdog.py").read_text(encoding="utf-8")
check("nothing must be RUN to switch it on - the watchdog starts the bridge",
      "def ensure_telegram_bridge" in _wd and "ensure_telegram_bridge()" in _wd,
      "inviting someone must not require a PowerShell command on the machine")
_auto_src = (ROOT / "tools" / "discord" / "autostart.ps1").read_text(encoding="utf-8")
check("...and it is not also a scheduled task - one supervisor, not two",
      "Name   = 'Omnius Telegram'" not in _auto_src)
check("a leftover task from the release that DID register one is removed",
      "Unregister-ScheduledTask -TaskName 'Omnius Telegram'" in _auto_src)
check("the lock is keyed by the BOT TOKEN and kept outside the workspace",
      "LOCALAPPDATA" in SRC and "sha256" in SRC,
      "one getUpdates consumer per BOT - two workspaces share a machine, not a folder")
check("the README states the contract", "Contract (stable regardless of engine)"
      in (HERE / "README.md").read_text(encoding="utf-8"))
check("every envelope write is atomic (temp + replace), never a torn read",
      ".json.tmp" in SRC and "tmp.replace" in SRC)
check("guests.ini is left alone - it gates DISCORD accounts, and these people "
      "have none", "guests.ini" not in SRC.replace("config\\\\guests.ini stays", ""))

_ast = __import__("ast").parse(SRC)
_writes_outbox = [n for n in __import__("ast").walk(_ast)
                  if isinstance(n, __import__("ast").Constant)
                  and isinstance(n.value, str) and n.value == "outbox"]
check("the word outbox appears in no path the bridge builds", not _writes_outbox)

shutil.rmtree(SAND, ignore_errors=True)
print(f"\n==== {_passed} passed, {_failed} failed ====")
sys.exit(1 if _failed else 0)
