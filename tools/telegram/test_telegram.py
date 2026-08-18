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
            {"id": "900000000000000001", "name": "general", "type": 0,
             "parent_id": "900000000000000000"},
            {"id": "900000000000000000", "name": "my-project", "type": 4},
        ]
        self.history = {}
        self._next = 700000000000000000

    def redact(self, t):
        return t

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
        return "600000000000000000"

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
        ("[chat.x]\ntelegram_user_id = 5\ndiscord_channel = g\n", "no desk"),
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
check("every rejection above was reported, not swallowed",
      any("telegram" in p.lower() for p in ocfg.problems()))

# --- telegram -> discord -------------------------------------------------------
print("\n== inbound ==")
api = FakeApi()
bridge._CHANNELS.clear()
bridge.tg = lambda token, method, params=None, files=None, timeout=None: (
    [msg(999999, "let me in")] if method == "getUpdates" else {"ok": True})
n = bridge.inbound(FAKE_TOKEN, CHATS, api)
check("an unlisted telegram id relays nothing", n == 0 and not api.posts)
check("...and writes no envelope, so no desk is even started",
      inbox("my-project.web") == [])
check("...and is answered with silence, not an explanation",
      not [s for s in _sent if s["method"] == "sendMessage"])
check("...but its id is logged, which is how you invite someone",
      "999999" in bridge.LOG.read_text(encoding="utf-8"))

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
      sorted(["600000000000000000.json", _env_names[0]])[0] == "600000000000000000.json")
check("channel and category are resolved, so the desk knows where it is",
      _env["channel"] == "general" and _env["category"] == "my-project")
check("channelId is the id, not the name", _env["channelId"] == "900000000000000001")
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
check("the bridge keeps its OWN cursor file", _cursor.get("900000000000000001"))
check("it never touches the watchdog's last_ids.json",
      "last_ids" not in CODE and not (SAND / "state" / "watchdog").exists(),
      "two cursors over one channel is fine; SHARING one is how the watchdog "
      "starts skipping the owner's messages")

api.history["900000000000000001"] = [
    {"id": "600000000000000001", "content": "owner private note",
     "author": {"id": "111", "username": "kneuma", "bot": False}, "attachments": []},
    {"id": "600000000000000002", "content": "here you go",
     "author": {"id": "222", "username": "omnius", "bot": True},
     "attachments": [{"filename": "report.pdf", "size": 10, "url": "http://x/y"}]},
]
_sent.clear()
bridge.outbound(FAKE_TOKEN, CHATS, api, "222")
_texts = [s["params"].get("text", "") for s in _sent]
check("visibility=own hides the owner's own messages",
      not any("owner private note" in t for t in _texts),
      f"leaked: {_texts}")
check("visibility=own still delivers Omnius's reply",
      any("here you go" in t for t in _texts))
check("the reply says who is speaking", any(t.startswith("Omnius:") for t in _texts))
check("media_out=0 names attachments instead of uploading them",
      any("report.pdf" in t for t in _texts)
      and not [s for s in _sent if s["method"] == "sendDocument"]
      and not api.downloads)

_all = {"antonio": dict(CHATS["antonio"], visibility="all")}
shutil.rmtree(bridge.STATE, ignore_errors=True)
bridge.outbound(FAKE_TOKEN, _all, api, "222")          # first pass = cursor only
bridge.write_cursor("mirror", {"900000000000000001": "600000000000000000"})
_sent.clear()
bridge.outbound(FAKE_TOKEN, _all, api, "222")
check("visibility=all does deliver the owner's messages",
      any("owner private note" in s["params"].get("text", "") for s in _sent))

# loop guard
bridge.write_cursor("mirror", {"900000000000000001": "600000000000000000"})
bridge.write_cursor("posted", {"ids": ["600000000000000002"]})
_sent.clear()
bridge.outbound(FAKE_TOKEN, _all, api, "222")
check("a message the bridge itself posted is never mirrored back",
      not any("here you go" in s["params"].get("text", "") for s in _sent),
      "without this every relayed message echoes forever")

# failure handling
bridge.write_cursor("mirror", {"900000000000000001": "600000000000000001"})
bridge.write_cursor("posted", {"ids": []})
_sent.clear()
_fail_next[0] = 1
bridge.outbound(FAKE_TOKEN, _all, api, "222")
_cur = json.loads((bridge.STATE / "mirror.json").read_text(encoding="utf-8"))
check("a failed mirror does NOT advance the cursor - it is retried, not lost",
      _cur["900000000000000001"] == "600000000000000001")
_fail_next[0] = 5
for _ in range(3):
    bridge.outbound(FAKE_TOKEN, _all, api, "222")
_cur = json.loads((bridge.STATE / "mirror.json").read_text(encoding="utf-8"))
check("after three failures it moves on rather than wedging the mirror",
      _cur["900000000000000001"] == "600000000000000002")
check("...and says so, so a dropped message is never silent",
      "gave up mirroring" in bridge.LOG.read_text(encoding="utf-8"))
_fail_next[0] = 0

# a long answer is split, not cut
_sent.clear()
bridge._send_to_telegram(FAKE_TOKEN, "555111", "x" * 9000, [])
check("a long desk answer is split across messages, not truncated",
      len(_sent) >= 3 and sum(len(s["params"]["text"]) for s in _sent) >= 9000)

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
