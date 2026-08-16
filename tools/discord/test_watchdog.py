#!/usr/bin/env python3
"""Offline regression tests for the Omnius watchdog + Discord helpers.

No Discord, no network: monkeypatches api network calls and isolates all state
into a temp sandbox. Run:  python tools\\discord\\test_watchdog.py
Exit 0 = all pass. Mirrors the ethos of daybook\\test_storage.py.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import pathlib
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import api          # noqa: E402
import watchdog as wd  # noqa: E402
import status_banner as sb  # noqa: E402

passed = failed = 0
def _raises(fn, *a):
    try:
        fn(*a); return False
    except Exception:
        return True

def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1; print(f"  [PASS] {name}")
    else:
        failed += 1; print(f"  [FAIL] {name}  {extra}")

SAND = Path(tempfile.mkdtemp(prefix="omnius-wdtest-"))
try:
    # --- isolate all filesystem touchpoints into the sandbox ------------------
    for sub in ("sessions", "inbox", "outbox", "media", "projects"):
        (SAND / sub).mkdir(parents=True)
    wd.ROOT = SAND
    wd.SESSIONS, wd.INBOX, wd.OUTBOX, wd.MEDIA, wd.LOGS = (
        SAND / "sessions", SAND / "inbox", SAND / "outbox", SAND / "media", SAND / "logs")
    wd.WD_STATE, wd.RUNS = SAND / "watchdog", SAND / "watchdog" / "runs"
    # Redirect EVERY state path up front. Setting one of these later means the
    # tests that run before it write into the real state\ directory.
    wd.TRANSCRIPTS, wd.PERMS = SAND / "transcripts", SAND / "permissions"
    wd.TURNS = SAND / "turns"
    # DROPPED was missed when this list was written, so !stop moved sandbox mail
    # into the LIVE state\dropped\ - and on this machine that is a cross-drive
    # rename (temp on C:, workspace on W:), which raises WinError 17 and failed
    # five checks for a reason that had nothing to do with them (2026-08-12).
    wd.DROPPED = SAND / "dropped"
    # Desk mail (docs\DELEGATION.md): chain ledgers + held cross-project asks.
    wd.THREADS, wd.GATE = SAND / "watchdog" / "threads", SAND / "gate"
    # fake project folders so build_map's folder-existence gate can resolve
    for comp in ("app", "backend"):
        (SAND / "projects" / "demo-app" / comp).mkdir(parents=True)

    # --- fake network / side effects -----------------------------------------
    # snowflake-shaped ids: config_problems() rejects anything that is not 17-20 digits
    api.TOKEN, api.GUILD, api.OWNER = "faketoken", "111111111111111111", "999999999999999999"
    sent, killed = [], []
    api.send_message = lambda cid, text, files=None: (sent.append((cid, text, files)) or [{"id": "x"}])
    wd.api.send_message = api.send_message
    def fake_kill(session):
        killed.append(session)
        p = wd.SESSIONS / f"{session}.json"; existed = p.exists(); p.unlink(missing_ok=True)
        return f"{session}: killed" if existed else f"{session}: nothing running"
    # Keep a handle on the real one too, for the same reason as spawn_session
    # below: a test of kill_session's own behaviour must not exercise the stub.
    _real_kill_session = wd.kill_session
    wd.kill_session = fake_kill
    spawned = []
    # Keep a handle on the real one: the argv and serialization tests must
    # exercise the actual function, not this stub, or they prove nothing.
    _real_start_run = wd.start_run
    # **kw so the stub survives new options (model/effort)
    wd.start_run = lambda session, **kw: (spawned.append(session) or True)

    class FakeProc:
        """Stands in for a start_run child: pid + poll()/returncode, settable."""
        def __init__(self, pid=1, rc=None):
            self.pid, self._rc = pid, rc
        def poll(self):
            return self._rc
        @property
        def returncode(self):
            return self._rc
    _real_process_image = wd.process_image
    # Desks here are simulated with THIS python process's pid, so the exe-name
    # check would reject every one. None means "image unreadable -> trust the
    # liveness result", which is exactly the pre-identity behaviour.
    wd.process_image = lambda pid: None
    api.add_reaction = lambda cid, mid, emoji=None: None
    def fake_download(url, dest):
        from pathlib import Path as _P
        d = _P(dest); d.parent.mkdir(parents=True, exist_ok=True); d.write_bytes(b"img")
        return d
    api.download = fake_download

    def now(off=0):
        return (datetime.now(timezone.utc) + timedelta(seconds=off)).strftime("%Y-%m-%dT%H:%M:%SZ")
    def claim(session, **kw):
        # watcherPid still accepted via kw for legacy-claim tests: kill_session
        # reads it from claims written before the run model.
        d = {"role": "project", "pid": None,
             "startedAt": now(), "lastSeenAt": now(),
             "machine": api.MACHINE, "discordChannel": session}
        d.update(kw)
        (wd.SESSIONS / f"{session}.json").write_text(json.dumps(d), encoding="utf-8")

    print("== session_alive ==")
    import os as _os0
    claim("fresh", pid=_os0.getpid())
    check("live claim pid -> alive", wd.session_alive("fresh"))
    claim("stale", pid=None)
    check("claim with no pid -> dead (no heartbeat can say otherwise)",
          not wd.session_alive("stale"))
    check("no claim -> dead", not wd.session_alive("ghost"))
    claim("deadpid", pid=99999999)
    check("dead pid -> dead, however fresh the lastSeenAt looks",
          not wd.session_alive("deadpid"))
    claim("elsewhere", pid=_os0.getpid(), machine="OTHER-PC")
    check("another machine's claim -> not alive here", not wd.session_alive("elsewhere"))
    wd.RUNS.mkdir(parents=True, exist_ok=True)
    wd.RUNNING["runner"] = FakeProc(pid=_os0.getpid())
    check("an active headless run -> alive with no claim at all", wd.session_alive("runner"))
    wd.RUNNING.clear()

    print("== chunk_text ==")
    check("empty -> ['']", api.chunk_text("") == [""])
    check("exactly 1990 -> 1 chunk", len(api.chunk_text("x" * 1990)) == 1)
    c = api.chunk_text("x" * 2500)
    check("2500 -> 2 chunks, all <= 1990", len(c) == 2 and all(len(x) <= 1990 for x in c))
    check("many newlines chunked", all(len(x) <= 1990 for x in api.chunk_text("a\n" * 2000)))
    fenced = "```py\n" + ("line\n" * 500) + "```"
    check("fence split keeps chunks balanced", all(x.count("```") % 2 == 0 for x in api.chunk_text(fenced)))

    print("== redact ==")
    check("exact token redacted", "faketoken" not in api.redact("x faketoken y"))
    botshape = "MTA5" + "A" * 20 + ".GABCDE.abcdefghijklmnopqrstuvwxyz1234567"
    check("bot-token shape redacted", "[redacted]" in api.redact("t " + botshape))
    check("normal text untouched", api.redact("hello world 123") == "hello world 123")

    # Found 2026-08-01 by the escalation test: the filter was ONLY the Discord
    # bot-token shape, i.e. it caught the one secret we happen to own and no
    # other. The permission relay posts the command line it is asking about
    # straight into a channel, so `curl -H "Authorization: Bot ..."` published a
    # live credential. Same path carries session output. CLAUDE.md par.5.
    for _label, _secret in [
        ("github PAT",        "ghp_abcdefghijklmnopqrstuvwxyz0123456789"),
        ("openai key",        "export OPENAI_API_KEY=sk-proj-abc123def456ghi789jkl012"),
        ("anthropic key",     "sk-ant-abc123def456ghi789jkl012mno"),
        ("google api key",    "AIzaSyD-abcdefghijklmnopqrstuvwxyz12345"),
        ("aws key id",        "AKIAIOSFODNN7EXAMPLE"),
        ("slack token",       "xoxb-123456789012-abcdefghijkl"),
        ("bearer header",     'curl -H "Authorization: Bearer abcdefghijklmnopqrstuvwxyz"'),
        ("named assignment",  "DB_PASSWORD=hunter2xyz"),
        ("private key block", "-----BEGIN RSA PRIVATE KEY-----"),
    ]:
        check(f"{_label} never reaches a channel", "[redacted]" in api.redact(_secret),
              api.redact(_secret))

    # A filter that mangles ordinary output gets switched off, and then it
    # protects nothing. Over-redaction is a real failure mode, not caution.
    for _label, _plain in [
        ("a commit hash",  "fix in commit a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"),
        ("a file path",    "C:/Users/x/omnius/tools/discord/watchdog.py"),
        ("PORT=8000",      "PORT=8000"),
        ("a test summary", "the build passed, 500 tests, 0 failed"),
    ]:
        check(f"{_label} survives untouched", api.redact(_plain) == _plain, api.redact(_plain))

    # The strongest rule available: whatever its shape, if it is OUR secret,
    # match it exactly. Guarded so PORT-like values cannot blank out numbers.
    _saved_env = dict(api.ENV)
    try:
        api.ENV.update({"SOME_API_KEY": "zqx-not-a-known-shape-9182", "PORT": "8000"})
        check("a value from .env is redacted whatever shape it has",
              "[redacted]" in api.redact("running with zqx-not-a-known-shape-9182"))
        check("...but a short/numeric .env value is not (PORT would blank out numbers)",
              api.redact("listening on 8000") == "listening on 8000")
    finally:
        api.ENV.clear(); api.ENV.update(_saved_env)

    print("== handle_control ==")
    class T:
        def __init__(self, ch, sess, cat=None):
            self.channel_name, self.session = ch, sess
            self.category_name = cat or (f"📁 {sess.split('.')[0]}" if sess and "." in sess else None)
    sent.clear(); killed.clear()
    claim("demo-app.app", lastSeenAt=now(-5)); claim("demo-app.backend", lastSeenAt=now(-5))
    wd.handle_control("!status", "C1", T("orchestrator", "orchestrator"), {})
    check("!status posts a fleet listing", bool(sent) and "fleet" in sent[-1][1].lower())
    check("!status lists sessions", "demo-app.app" in sent[-1][1])
    sent.clear()
    wd.handle_control("!kill", "C1", T("app", "demo-app.app"), {})
    check("!kill kills the channel's session", "demo-app.app" in killed)
    sent.clear(); killed.clear()
    wd.handle_control("!restart", "C1", T("backend", "demo-app.backend"), {})
    check("!restart kills then starts a fresh run",
          "demo-app.backend" in killed and "fresh run" in sent[-1][1].lower()
          and spawned[-1:] == ["demo-app.backend"])
    spawned.clear()
    sent.clear(); killed.clear()
    claim("demo-app.app"); claim("demo-app.backend")
    wd.handle_control("!killall", "C1", T("orchestrator", "orchestrator"), {})
    check("!killall from #orchestrator kills all", len(killed) >= 2)
    sent.clear(); killed.clear()
    wd.handle_control("!killall", "C1", T("app", "demo-app.app"), {})
    # Assert the behaviour plus that the refusal points somewhere REAL. The old
    # check matched the literal word "orchestrator", so it went red when the
    # message was corrected to name #omnius after the 2026-07-31 rename - the
    # test was pinning stale wording rather than protecting anything.
    check("!killall refused outside the orchestrator door",
          killed == [] and "omnius" in sent[-1][1].lower())
    sent.clear()
    wd.handle_control("!kill", "C1", T("fleet-status", None), {})
    check("!kill on session-less channel handled", bool(sent) and "no session" in sent[-1][1].lower())

    print("== flush_outboxes (+ media archive) ==")
    sent.clear()
    (wd.OUTBOX / "demo-app.app").mkdir(parents=True)
    asset = SAND / "shot.png"; asset.write_bytes(b"\x89PNG fake")
    (wd.OUTBOX / "demo-app.app" / "1700000000000.json").write_text(
        json.dumps({"text": "shot", "channel": "app", "files": [str(asset)]}), encoding="utf-8")
    mapping = {"CID_APP": T("app", "demo-app.app")}
    wd.flush_outboxes(mapping)
    check("outbox posted to mapped channel", bool(sent) and sent[-1][0] == "CID_APP")
    check("outbox file deleted after post", not list((wd.OUTBOX / "demo-app.app").glob("*.json")))
    check("sent file archived to media/sent", len(list((wd.MEDIA / "sent").rglob("shot.png"))) == 1)
    (wd.OUTBOX / "demo-app.app" / "bad.json").write_text("{not json", encoding="utf-8")
    wd.flush_outboxes(mapping)
    check("corrupt outbox renamed .bad, no crash", bool(list((wd.OUTBOX / "demo-app.app").glob("*.bad"))))

    print("== build_map ==")
    api.guild_channels = lambda: [
        {"id": "cat_o", "type": 4, "name": "\U0001f39b ORCHESTRATOR", "position": 0},
        {"id": "c_orch", "type": 0, "name": "orchestrator", "parent_id": "cat_o", "position": 0},
        {"id": "c_day", "type": 0, "name": "daybook", "parent_id": "cat_o", "position": 1},
        {"id": "c_fs", "type": 0, "name": "fleet-status", "parent_id": "cat_o", "position": 2},
        {"id": "c_tr", "type": 0, "name": "transcribe", "parent_id": "cat_o", "position": 3},
        {"id": "cat_e", "type": 4, "name": "\U0001f4e7 EMAIL", "position": 3},
        {"id": "c_eg", "type": 0, "name": "email-gmail", "parent_id": "cat_e", "position": 0},
        {"id": "c_ef", "type": 0, "name": "email-work", "parent_id": "cat_e", "position": 1},
        {"id": "c_enew", "type": 0, "name": "email-whatever", "parent_id": "cat_e", "position": 2},
        {"id": "cat_p", "type": 4, "name": "\U0001f4c1 demo-app", "position": 1},
        {"id": "c_gen", "type": 0, "name": "general", "parent_id": "cat_p", "position": 0},
        {"id": "c_app", "type": 0, "name": "app", "parent_id": "cat_p", "position": 1},
        {"id": "c_be", "type": 0, "name": "backend", "parent_id": "cat_p", "position": 2},
        {"id": "c_ghost", "type": 0, "name": "ghostcomp", "parent_id": "cat_p", "position": 3},
        {"id": "cat_a", "type": 4, "name": "\U0001f5c4 old-proj", "position": 2},
        {"id": "c_old", "type": 0, "name": "app", "parent_id": "cat_a", "position": 0},
    ]
    m = wd.build_map(api.load_schema())
    sess = {t.channel_name: t.session for t in m.values()}
    check("#orchestrator -> orchestrator", sess.get("orchestrator") == "orchestrator")
    # Changed 2026-07-31: #daybook used to map to the orchestrator. It now has its
    # own desk so note capture never occupies the fleet coordinator.
    check("#daybook -> its own daybook session", sess.get("daybook") == "daybook")
    # Changed 2026-07-31: #fleet-status was an unowned board. It now has a
    # read-only desk so fleet questions can be asked in natural language without
    # queueing behind the orchestrator. The watchdog still posts its board there.
    check("#fleet-status -> tool.fleet", sess.get("fleet-status") == "tool.fleet")
    # Added 2026-08-06: the recordings desk. Transcribing two hours takes ~25
    # minutes and reading it back costs ~40k tokens - neither may happen inside
    # the orchestrator, or he loses Omnius for the duration.
    check("#transcribe -> tool.transcribe", sess.get("transcribe") == "tool.transcribe")
    # A category may claim every channel in it for one desk (2026-08-06, email).
    # The third channel is the point: it is in NO schema and NO config, and it
    # still routes - which is what makes "add an account, get a channel" need no
    # code change. The envelope's channelId is what sends the reply back to the
    # right account.
    check("#email-gmail -> tool.email", sess.get("email-gmail") == "tool.email")
    check("#email-work -> the SAME desk (N channels, one session)",
          sess.get("email-work") == "tool.email")
    check("...and an account added later routes with no code change",
          sess.get("email-whatever") == "tool.email")
    check("cwd_for tool.email is the tool folder",
          wd.cwd_for("tool.email") == SAND / "tools" / "email")
    check("cwd_for tool.transcribe is the tool folder",
          wd.cwd_for("tool.transcribe") == SAND / "tools" / "transcribe")
    check("tool.transcribe takes the tool role", wd.role_of("tool.transcribe") == "tool")
    check("#general -> orchestrator (relayed)", sess.get("general") == "orchestrator")
    check("#app -> demo-app.app (folder exists)", sess.get("app") == "demo-app.app")
    check("#backend -> demo-app.backend", sess.get("backend") == "demo-app.backend")
    check("#ghostcomp -> None (no folder)", sess.get("ghostcomp") is None)
    check("archived category not mapped", "c_old" not in m)

    print("== helpers ==")
    check("cwd_for orchestrator == ROOT", wd.cwd_for("orchestrator") == wd.ROOT)
    check("cwd_for project.component", wd.cwd_for("demo-app.app") == SAND / "projects" / "demo-app" / "app")
    check("primary_channel_id app", wd.primary_channel_id(m, "demo-app.app") == "c_app")
    check("primary_channel_id orchestrator", wd.primary_channel_id(m, "orchestrator") == "c_orch")

    print("== handle_message (inbound dispatch) ==")
    me = {"id": "BOT"}
    def msg(author_id, content, bot=False, atts=None):
        return {"id": "1", "author": {"id": author_id, "bot": bot},
                "content": content, "attachments": atts or [], "timestamp": now()}
    spawned.clear()
    check("bot message skipped", wd.handle_message(msg("999999999999999999", "hi", bot=True), "C", T("app", "demo-app.app"), me, {}) == "skip-bot")
    check("own message skipped", wd.handle_message(msg("BOT", "hi"), "C", T("app", "demo-app.app"), me, {}) == "skip-bot")
    check("non-owner skipped", wd.handle_message(msg("777", "hi"), "C", T("app", "demo-app.app"), me, {}) == "skip-nonowner")
    check("no envelope from skipped senders", not list((wd.INBOX / "demo-app.app").glob("*.json")) if (wd.INBOX / "demo-app.app").exists() else True)
    sent.clear()
    check("owner control routed", wd.handle_message(msg("999999999999999999", "!status"), "C", T("orchestrator", "orchestrator"), me, {}) == "control")
    sent.clear()
    check("owner msg to unmapped channel", wd.handle_message(
        msg("999999999999999999", "hi"), "C", T("fleet-status", None), me,
        {"CID_O": T("orchestrator", "orchestrator")}) == "unmapped")
    check("unmapped channel -> owner gets a redirect", bool(sent) and "#orchestrator" in sent[-1][1])
    # An IDLE terminal on the desk (claim alive, no busy stamp) does NOT block
    # the run: an idle terminal cannot be woken externally, so the run continues
    # the same conversation. Only a terminal MID-TURN (busy stamp) holds it off.
    claim("demo-app.backend", pid=_os0.getpid())
    spawned.clear()
    r = wd.handle_message(msg("999999999999999999", "build the thing"), "C", T("backend", "demo-app.backend"), me, {})
    check("owner msg with an IDLE terminal on the desk -> run starts anyway",
          r == "spawned" and spawned == ["demo-app.backend"])
    check("envelope written for delivered", len(list((wd.INBOX / "demo-app.backend").glob("*.json"))) == 1)
    wd.TURNS.mkdir(parents=True, exist_ok=True)
    (wd.TURNS / "demo-app.backend.busy").write_text("{}", encoding="utf-8")
    spawned.clear()
    r = wd.handle_message(msg("999999999999999999", "and another thing"), "C", T("backend", "demo-app.backend"), me, {})
    check("owner msg while that terminal is MID-TURN -> queued, no run",
          r == "queued" and spawned == [])
    (wd.TURNS / "demo-app.backend.busy").unlink()
    (wd.SESSIONS / "demo-app.backend.json").unlink(missing_ok=True)
    for f in (wd.INBOX / "demo-app.backend").glob("*.json"):
        f.unlink()
    (wd.SESSIONS / "demo-app.app.json").unlink(missing_ok=True)
    spawned.clear()
    r = wd.handle_message(msg("999999999999999999", "wake up"), "C", T("app", "demo-app.app"), me, {})
    check("owner msg to DEAD session -> spawned", r == "spawned" and spawned == ["demo-app.app"])
    r = wd.handle_message(msg("999999999999999999", "look", atts=[{"filename": "shot.png", "url": "http://x/shot.png", "content_type": "image/png"}]),
                          "C", T("backend", "demo-app.backend"), me, {})
    envs = sorted((wd.INBOX / "demo-app.backend").glob("*.json"))
    last = json.loads(envs[-1].read_text(encoding="utf-8"))
    check("attachment saved into envelope + media/inbox", last["files"] and "media" in last["files"][0]["path"])

    # A control-LOOKING word is not a control. "!killswitch ideas" prefix-
    # matches !kill and "!modelo fable" prefix-matches !model, and until
    # 2026-08-16 the dispatch used startswith(CONTROL_COMMANDS), so both
    # entered a chain where no branch fired - the message VANISHED: no action,
    # no reply, no envelope. (His "!goal ..." of the same day survived only
    # because !goal shares no prefix with a real verb.) Exact token, or mail.
    spawned.clear()
    _m = msg("999999999999999999", "!killswitch ideas for the launch")
    _m["id"] = "20260816001"
    _before = {p.name for p in (wd.INBOX / "demo-app.backend").glob("*.json")}
    r = wd.handle_message(_m, "C", T("backend", "demo-app.backend"), me, {})
    check("a !word that only PREFIXES a verb is mail, not control",
          r in ("spawned", "queued", "delivered"))
    _new = {p.name for p in (wd.INBOX / "demo-app.backend").glob("*.json")} - _before
    check("...its envelope is written (nothing swallowed)", len(_new) == 1)
    _kenv = (json.loads((wd.INBOX / "demo-app.backend" / next(iter(_new))).read_text(encoding="utf-8"))
             if _new else {})
    check("...text intact for the desk to read",
          _kenv.get("text", "").startswith("!killswitch"))
    _m2 = msg("999999999999999999", "!modelo fable para este desk")
    _m2["id"] = "20260816002"
    _fj_before = wd.FLEET_CFG.read_text(encoding="utf-8") if wd.FLEET_CFG.is_file() else None
    r = wd.handle_message(_m2, "C", T("backend", "demo-app.backend"), me, {})
    check("Spanish '!modelo ...' does not fire !model",
          r in ("spawned", "queued", "delivered"))
    _fj_after = wd.FLEET_CFG.read_text(encoding="utf-8") if wd.FLEET_CFG.is_file() else None
    check("...and fleet.json is untouched", _fj_before == _fj_after)
    sent.clear()
    check("the exact verb still routes to control",
          wd.handle_message(msg("999999999999999999", "!status"), "C",
                            T("orchestrator", "orchestrator"), me, {}) == "control")
    # And if the tuple and the chain ever drift apart (a verb listed but never
    # wired), the owner is TOLD - a swallowed control is the failure he cannot
    # distinguish from a dead fleet.
    _saved_cc = wd.CONTROL_COMMANDS
    wd.CONTROL_COMMANDS = _saved_cc + ("!ghostverb",)
    sent.clear()
    r = wd.handle_message(msg("999999999999999999", "!ghostverb now"), "C",
                          T("backend", "demo-app.backend"), me, {})
    check("a listed verb with no handler ANSWERS instead of swallowing",
          r == "control" and bool(sent) and "wiring bug" in sent[-1][1])
    wd.CONTROL_COMMANDS = _saved_cc
    # Tidy only what THIS block wrote - the guests section below counts on the
    # earlier deliveries staying in the box.
    for _n in ("20260816001.json", "20260816002.json"):
        (wd.INBOX / "demo-app.backend" / _n).unlink(missing_ok=True)

    print("== guests: a second person on the bus ==")
    # Until 2026-08-12 handle_message dropped EVERY non-owner message, silently,
    # so the artist whose brand a project is being built for had no way to talk
    # to that project's desk - and the desk could not have told her apart if she
    # had, because write_envelope stamped "owner" on everything. Most of what
    # follows tests the refusals: the boundary IS the feature.
    # Desks of their own: the deliveries above left envelopes that LATER tests
    # depend on (the terminal-tab branch only fires when a person's mail is
    # waiting), so a guest test that tidies up after itself must not tidy up
    # after them.
    _guest_id = "424242424242424242"
    _g = {"id": _guest_id, "name": "Guestina", "channels": ["c_ux"], "scope": "ux"}
    _saved_guests = wd.GUESTS
    wd.GUESTS = {"nina": dict(_g)}
    spawned.clear()
    sent.clear()
    r = wd.handle_message(msg(_guest_id, "here are my colour ideas"), "c_ux",
                          T("ux", "demo-app.ux"), me, {})
    check("a guest writing in HER channel is delivered", r in ("spawned", "queued", "delivered"))
    _envs = sorted((wd.INBOX / "demo-app.ux").glob("*.json"))
    _genv = json.loads(_envs[-1].read_text(encoding="utf-8")) if _envs else {}
    check("the envelope names WHO wrote it", _genv.get("from") == "nina")
    check("...and no longer lies that it was him", _genv.get("from") != "owner")
    # Confined to her own channels, and refused SILENTLY: answering "you may not
    # write here" in a channel she was never meant to reach would draw her a map
    # of the fleet.
    sent.clear()
    spawned.clear()
    r = wd.handle_message(msg(_guest_id, "and over here"), "c_other",
                          T("other", "demo-app.other"), me, {})
    check("a guest in ANOTHER desk's channel is refused", r == "skip-guest-channel")
    check("...silently - nothing is posted back", sent == [])
    check("...and no envelope is written",
          not list((wd.INBOX / "demo-app.other").glob("*.json"))
          if (wd.INBOX / "demo-app.other").exists() else True)
    sent.clear()
    r = wd.handle_message(msg(_guest_id, "hello?"), "CID_F", T("fleet-status", None), me,
                          {"CID_O": T("orchestrator", "orchestrator")})
    check("a guest in an unmapped channel is refused silently too - no channel list leaked",
          r == "skip-guest-channel" and sent == [])
    # Control verbs are HIS. !screen would hand her a screenshot of his desktop.
    for f in (wd.INBOX / "demo-app.ux").glob("*.json"):
        f.unlink()
    sent.clear()
    r = wd.handle_message(msg(_guest_id, "!screen"), "c_ux", T("ux", "demo-app.ux"), me, {})
    check("a guest's !screen is NOT run as a control verb", r != "control")
    _genv = json.loads(sorted((wd.INBOX / "demo-app.ux").glob("*.json"))[-1]
                       .read_text(encoding="utf-8"))
    check("...it is delivered as ordinary mail from her",
          _genv.get("text") == "!screen" and _genv.get("from") == "nina")
    _gsrc = (HERE / "watchdog.py").read_text(encoding="utf-8")
    check("control verbs, permission answers and takeover answers are gated on the owner",
          'if sender == "owner":' in _gsrc
          and _gsrc.index('if sender == "owner":') < _gsrc.index("verdict = answer_permission(text)")
          and _gsrc.index('if sender == "owner":') < _gsrc.index("takeover = answer_takeover("))
    # A channel NAME works too (what a human types), though ids are what the
    # template recommends - every project may have a #web.
    wd.GUESTS = {"nina": dict(_g, channels=["ux"])}
    for f in (wd.INBOX / "demo-app.ux").glob("*.json"):
        f.unlink()
    r = wd.handle_message(msg(_guest_id, "by channel name"), "CID_X",
                          T("ux", "demo-app.ux"), me, {})
    check("a channel NAME in the guest's list also matches",
          r in ("spawned", "queued", "delivered"))
    wd.GUESTS = {}
    check("with no guests configured, a stranger is dropped exactly as before",
          wd.handle_message(msg(_guest_id, "hello?"), "c_ux",
                            T("ux", "demo-app.ux"), me, {}) == "skip-nonowner")
    # The reader fails closed. A generous reading of a typo is how an
    # authorisation list becomes a hole.
    import omnius_config as _gcfg                                     # noqa: E402
    _greal = _gcfg.load
    _gfake = {
        "guest.nina": {"name": "Guestina", "user_id": _guest_id,
                       "channels": "c_app, c_web", "scope": "ux"},
        "guest.nochannels": {"user_id": "424242424242424243"},
        "guest.badid": {"user_id": "not-an-id", "channels": "c_app"},
        "guest.owner": {"user_id": "424242424242424244", "channels": "c_app"},
    }
    try:
        # Only "guests" is faked - answering every config file with guest
        # sections would contaminate whatever else asks while this is patched.
        _gcfg.load = lambda name, legacy=None: (
            _gfake if name == "guests" else _greal(name, legacy))
        _read = _gcfg.guests()
        check("a complete guest entry is read", _read.get("nina", {}).get("id") == _guest_id)
        check("...channels split on commas and spaces alike",
              _read["nina"]["channels"] == ["c_app", "c_web"])
        check("a guest with NO channels is ignored, never handed all of them",
              "nochannels" not in _read)
        check("a user_id that is not a snowflake is ignored, not treated as a wildcard",
              "badid" not in _read)
        check("a guest may not wear a system sender's name", "owner" not in _read)
        check("every rejection is reported, not swallowed",
              sum(1 for p in _gcfg.problems() if "guests.ini" in p) >= 3)
    finally:
        _gcfg.load = _greal
    wd.GUESTS = _saved_guests
    for f in (wd.INBOX / "demo-app.ux").glob("*.json"):
        f.unlink()

    print("== ensure_project ==")
    real_schema = api.load_schema()
    api.load_schema = lambda: real_schema   # ROOT is about to move into the sandbox
    api.ROOT = SAND  # project_components reads ROOT/projects
    created_cats, created_chans = [], []
    api.create_category = lambda name: (created_cats.append(name) or {"id": f"cat_{len(created_cats)}", "type": 4, "name": name})
    api.create_text_channel = lambda name, parent, topic="": (created_chans.append((name, topic)) or {"id": f"ch_{len(created_chans)}", "type": 0, "name": name, "parent_id": parent})
    fake_guild = [{"id": "cat_o", "type": 4, "name": "\U0001f39b ORCHESTRATOR", "position": 0}]
    api.guild_channels = lambda: list(fake_guild)
    check("project_components finds comps, skips memory",
          api.project_components("demo-app") == ["app", "backend"])
    comps = api.ensure_project("demo-app", log=lambda *a: None)
    check("ensure_project returns components", comps == ["app", "backend"])
    check("ensure_project creates the category", created_cats == ["\U0001f4c1 demo-app"])
    check("ensure_project creates general + per-component channels",
          [n for n, _ in created_chans] == ["general", "app", "backend"])
    check("component topic carries the path", any("projects\\demo-app\\app" in t for n, t in created_chans if n == "app"))
    # idempotent second run: feed back what exists -> nothing new created
    fake_guild = [{"id": "cat_p", "type": 4, "name": "\U0001f4c1 demo-app", "position": 0}] + [
        {"id": f"c{i}", "type": 0, "name": n, "parent_id": "cat_p"} for i, (n, _) in enumerate(created_chans)]
    created_cats.clear(); created_chans.clear()
    api.ensure_project("demo-app", log=lambda *a: None)
    check("ensure_project idempotent (no dupes)", not created_cats and not created_chans)
    try:
        api.project_components("no-such-project")
        check("project_components rejects missing project", False)
    except api.ApiError:
        check("project_components rejects missing project", True)

    print("== ensure_trusted ==")
    wd.CLAUDE_CFG = SAND / "claude.json"
    wd.CLAUDE_CFG.write_text(json.dumps({"projects": {}}), encoding="utf-8")
    check("ensure_trusted stamps a new folder", wd.ensure_trusted(SAND / "projects" / "demo-app"))
    cfg = json.loads(wd.CLAUDE_CFG.read_text(encoding="utf-8"))
    key = str(SAND / "projects" / "demo-app").replace("\\", "/")
    check("trust flag persisted with forward-slash key",
          cfg["projects"].get(key, {}).get("hasTrustDialogAccepted") is True)
    check("ensure_trusted idempotent", wd.ensure_trusted(SAND / "projects" / "demo-app"))
    check("other config keys preserved", "projects" in cfg)

    print("== #omnius rename (transition-safe) ==")
    _wsrc = (HERE / "watchdog.py").read_text(encoding="utf-8")
    check("build_map accepts BOTH #omnius and #orchestrator",
          'name in ("omnius", "orchestrator")' in _wsrc)
    check("!killall accepts either name", 'not in ("omnius", "orchestrator")' in _wsrc)
    _sch = json.loads((HERE / "schema.json").read_text(encoding="utf-8"))
    _names = [c["name"] for cat in _sch["initial"]["categories"] for c in cat["channels"]]
    check("schema stamps #omnius, not #orchestrator", "omnius" in _names and "orchestrator" not in _names)
    check("schema gives #daybook its own session",
          any(c["name"] == "daybook" and c.get("session") == "daybook"
              for cat in _sch["initial"]["categories"] for c in cat["channels"]))
    check("schema gives #fleet-status to tool.fleet",
          any(c["name"] == "fleet-status" and c.get("session") == "tool.fleet"
              for cat in _sch["initial"]["categories"] for c in cat["channels"]))
    # A FRESH INSTALL must get #transcribe too, or the desk exists only on this
    # machine and the next one silently loses the capability. ensure_structure()
    # find-or-creates by exact name from this schema, so being listed here IS
    # the guarantee - and it must sit in the ORCHESTRATOR category, whose
    # channel names are the ones build_map() resolves to sessions.
    _orch_cat = _sch["initial"]["categories"][0]
    check("schema stamps #transcribe on a fresh install",
          any(c["name"] == "transcribe" and c.get("session") == "tool.transcribe"
              for c in _orch_cat["channels"]))
    check("#transcribe is stamped inside the ORCHESTRATOR category",
          "ORCHESTRATOR" in _orch_cat["name"])
    # Email channels are NOT listed in the schema - they are derived from
    # config/email.ini, so adding an account is the only step (owner, 2026-08-06).
    _email_cat = next((c for c in _sch["initial"]["categories"]
                       if c.get("session") == "tool.email"), None)
    check("schema declares an EMAIL category owned by tool.email", _email_cat is not None)
    check("...whose channels come from config, not from a hardcoded list",
          bool(_email_cat and _email_cat.get("channelsFrom", {}).get("config") == "email"
               and not _email_cat.get("channels")))
    _espec = {"config": "email", "group": "account", "nameFrom": "user", "prefix": ""}
    check("...naming the channel after the ADDRESS, not the config label",
          _email_cat.get("channelsFrom", {}).get("nameFrom") == "user")
    check("a broken/missing config yields no channels rather than raising",
          api.config_channels({"config": "nosuchfile", "group": "account"},
                              log=lambda *a: None) == []
          and api.config_channels(None, log=lambda *a: None) == [])

    # His question, 2026-08-06: "what if i add a second gmail, or three?" A label
    # like `gmail` stops meaning anything at two. The address always distinguishes
    # them - and the TLD is dropped as noise but MUST come back when dropping it
    # would collide, which is the case that would otherwise fail silently by
    # find-or-create'ing one channel for two accounts.
    sys.path.insert(0, str(HERE.parent.parent / "tools"))
    import omnius_config as _oc
    _real_group = _oc.group
    def _names(fake):
        _oc.group = lambda cfg, prefix, _f=fake: _f
        try:
            return [c["name"] for c in api.config_channels(_espec, log=lambda *a: None)]
        finally:
            _oc.group = _real_group
    # Neutral fixtures on purpose: this file is tracked, and pack.ps1 -Fresh
    # refuses a release carrying a real address (tools\release_sanitize.py).
    check("two accounts on one provider get distinct channels",
          _names({"a": {"user": "alice@gmail.com"},
                  "b": {"user": "alice.work@gmail.com"}})
          == ["alice-gmail", "alice-work-gmail"])
    check("...and the TLD comes back only when dropping it would collide",
          _names({"es": {"user": "x@example.es"},
                  "com": {"user": "x@example.com"}})
          == ["x-example-com", "x-example-es"])
    check("...an account with no address falls back to its label, not to nothing",
          _names({"weird": {"host": "imap.example.com"}}) == ["weird"])
    check("...and '@' never reaches a Discord channel name",
          all("@" not in n and n == n.lower()
              for n in _names({"a": {"user": "Alice.Smith@Gmail.COM"}})))
    # The real hazard: every project #general also maps to session "orchestrator",
    # so an any-channel fallback could answer the owner inside a project channel.
    _mix = {"CID_GEN": T("general", "orchestrator"), "CID_OMN": T("omnius", "orchestrator")}
    check("primary_channel_id prefers #omnius over a project #general",
          wd.primary_channel_id(_mix, "orchestrator") == "CID_OMN")
    _old = {"CID_GEN": T("general", "orchestrator"), "CID_ORCH": T("orchestrator", "orchestrator")}
    check("...and still finds #orchestrator before the rename happens",
          wd.primary_channel_id(_old, "orchestrator") == "CID_ORCH")

    _api_src = (HERE / "api.py").read_text(encoding="utf-8")
    check("api.py has set-name (each instance gets its own bot and will want it)",
          '"set-name"' in _api_src and 'body={"username"' in _api_src)
    check("set-name documents that identity is ids, not the name",
          "self-message detection compares user IDs" in _api_src)

    print("== autostart (Task Scheduler) ==")
    # The task definitions live in autostart.ps1. They used to live inline in
    # install.ps1, and the copy on the live machine silently drifted from it
    # (2026-08-01: still launching python.exe, still pointing at the old root).
    # ONE owner, re-runnable on its own - that is what these tests pin.
    _auto = (HERE / "autostart.ps1").read_text(encoding="utf-8")
    _inst = (HERE.parent.parent / "install.ps1").read_text(encoding="utf-8")
    check("autostart.ps1 registers a scheduled task, not just a Startup shortcut",
          "Register-ScheduledTask" in _auto)
    # A Startup shortcut can only start things; restart-on-failure is the point.
    check("task restarts the service on failure",
          "-RestartCount" in _auto and "-RestartInterval" in _auto)
    check("task is never killed for running too long (it is a daemon)",
          "ExecutionTimeLimit ([TimeSpan]::Zero)" in _auto)
    # start-omnius.bat launches panes and EXITS, so it can never be the task action -
    # the task would complete instantly and restart-on-failure would never fire.
    check("task runs the service script, not start-omnius.bat",
          "watchdog.py" in _auto and "start-omnius" not in _auto.split("# --- actions")[0])
    # Measured 2026-08-01: RestartCount/RestartInterval did NOT bring a killed
    # watchdog back (task went Ready, result 0x1, no next run). The self-heal is
    # a repeating TIME trigger - a repeating logon trigger only runs inside the
    # window logon opened, so it does nothing for a task registered after logon.
    # Verified after the fix: killed -> back on its own in 21s.
    check("task self-heals via a repeating time trigger, not just restart-on-failure",
          "MSFT_TaskRepetitionPattern" in _auto and "PT1M" in _auto
          and "New-ScheduledTaskTrigger -Once" in _auto)
    check("status rejects a logon-only repetition as no self-heal",
          "MSFT_TaskTimeTrigger" in _auto)
    # A console app under Task Scheduler puts a window on the desktop at logon,
    # and closing that window kills the service.
    check("services start hidden (pythonw, not python)", "pythonw.exe" in _auto)
    check("autostart can be checked and repaired without the installer",
          "'status', 'install', 'repair', 'uninstall'" in _auto)
    check("autostart.ps1 is non-interactive (installer owns the prompt)",
          "Read-Host" not in _auto)
    # The workspace moves between machines; the registered task cannot follow it
    # on its own because a task action is necessarily an absolute path.
    check("status detects a task pointing at another workspace",
          "points at a different workspace/script" in _auto)
    check("both long-running services get a task, not just the watchdog",
          "daybook\\app.py" in _auto)
    # With a 1-minute self-heal trigger and IgnoreNew, "the operator refused the
    # request" (0x800710E0) is the HEALTHY steady state, reported every minute.
    # Left as a raw error code it makes every status check look half-broken.
    # LastTaskResult comes back as a UInt32 (2147946720), not the signed form
    # this code is usually written with - matching only the signed one silently
    # never fires, which is exactly what happened first time round.
    check("status reads the every-minute 'refused' result as healthy, not as a fault",
          "2147946720" in _auto and "already running" in _auto)
    check("installer delegates to autostart.ps1 instead of duplicating it",
          "autostart.ps1" in _inst and "New-ScheduledTaskAction" not in _inst)
    check("autostart is offered, never silently imposed", "register / repair autostart" in _inst)
    check("registration failure is caught and explained, not fatal",
          "could not enable autostart" in _inst)

    # playwright (2026-08-07): headless browsing as a default tool. The pip
    # package always; the ~150MB browser only if ASKED - a fresh install on a
    # tethered connection must not silently pull a Chromium.
    check("install.ps1 installs playwright by default",
          "pip install --quiet playwright" in _inst)
    check("...but ASKS before the 150MB browser download",
          "Ask-Install 'the Chromium build" in _inst)
    check("...and a re-run stays silent once the browser is cached",
          "ms-playwright" in _inst)
    check("...telling you the command if you decline, so it is recoverable",
          "python -m playwright install chromium" in _inst)
    _pw = (HERE.parent / "playwright" / "README.md").read_text(encoding="utf-8")
    _pwflat = " ".join(_pw.split())
    check("the playwright README states which browser tool to use when",
          "Chrome extension" in _pw and "Playwright" in _pw)
    check("...and forbids working around it with a scripted login",
          "Never work around the split" in _pwflat
          and "credentials in a Playwright script" in _pwflat)
    _fetch = (HERE.parent / "playwright" / "fetch.py").read_text(encoding="utf-8")
    check("fetch.py explains itself instead of tracebacking when unavailable",
          "playwright is not installed" in _fetch)

    # crawl.py (2026-08-09) - browsing and crawling are regular work now.
    sys.path.insert(0, str(HERE.parent / "playwright"))
    import crawl as _crawl                                        # noqa: E402
    check("norm() folds /a, /a/ and /a#x into one page",
          _crawl.norm("https://x.io/a") == _crawl.norm("https://x.io/a/")
          == _crawl.norm("https://x.io/a#top"))
    check("...and keeps the query, which usually IS a different page",
          _crawl.norm("https://x.io/a?p=2") != _crawl.norm("https://x.io/a"))
    check("...and refuses mailto:/javascript: links",
          _crawl.norm("mailto:a@example.com") is None
          and _crawl.norm("javascript:void(0)") is None)
    check("same_site keeps the crawl on one host by default",
          _crawl.same_site("https://x.io/a", "x.io", False)
          and not _crawl.same_site("https://sub.x.io/a", "x.io", False))
    check("...and --allow-subdomains widens it to subdomains only",
          _crawl.same_site("https://sub.x.io/a", "x.io", True)
          and not _crawl.same_site("https://evil-x.io/a", "x.io", True),
          "endswith without the dot would match evil-x.io")
    _cr = (HERE.parent / "playwright" / "crawl.py").read_text(encoding="utf-8")
    # THE bug: RobotFileParser.read() fetches as Python-urllib, many WAFs 403 it,
    # and on 403 it sets disallow_all - stricter than RFC 9309, and silent. A
    # crawl of a site that welcomes crawlers returned exactly one page.
    check("crawl.py does NOT use RobotFileParser.read()",
          "rp.read()" not in _cr and ".read()\n" not in _cr.replace("r.read()", ""),
          "it judges you on a request the site refused")
    check("...it fetches robots.txt with a real user-agent and parses it itself",
          "rp.parse(" in _cr and 'headers={"User-Agent": ua}' in _cr)
    check("...and treats 5xx differently from 4xx (RFC 9309)",
          "500 <= e.code < 600" in _cr)
    check("...and the same UA drives the browser, so rules match the request",
          "new_context(user_agent=args.user_agent)" in _cr)
    check("the run always reports what robots.txt decided",
          "robots.txt: {robots_note}" in _cr, "the bug it replaced was silent")
    check("concurrency defaults to a polite 3",
          '"--concurrency", type=int, default=3' in _cr)
    check("crawl.py SAVES pages and prints only a map (a desk's context is finite)",
          "NOT printed here on purpose" in _cr)
    check("...and says what it did NOT reach when it hits the ceiling",
          "stopped at --max-pages" in _cr,
          "silent truncation reads as 'that is the whole site'")

    # pythonw leaves sys.stdout/sys.stderr as None: prints vanish and an
    # unhandled traceback dies with the process. The runner gives them a file.
    _runner = (HERE.parent / "service_runner.py").read_text(encoding="utf-8")
    check("service_runner gives a hidden service real log streams",
          "sys.stdout = sys.stderr = stream" in _runner)
    check("service_runner runs the target as __main__",
          'run_name="__main__"' in _runner)
    check("service_runner propagates the exit code (restart-on-failure needs it)",
          "except SystemExit" in _runner and "return code" in _runner)
    check("service_runner caps its log like watchdog.rotate_log does",
          "MAX_BYTES" in _runner)

    print("== fleet desk (tool.fleet) ==")
    check("cwd_for('tool.fleet') is <root>\\tools\\fleet",
          wd.cwd_for("tool.fleet") == wd.ROOT / "tools" / "fleet")
    _fleet = HERE.parent.parent / "tools" / "fleet"
    check("fleet desk exists with a README defining its scope", (_fleet / "README.md").is_file())
    check("fleet desk has settings.json", (_fleet / ".claude" / "settings.json").is_file())
    check("fleet desk has the omnius skill stub",
          (_fleet / ".claude" / "skills" / "omnius" / "SKILL.md").is_file())
    _fs = json.loads((_fleet / ".claude" / "settings.json").read_text(encoding="utf-8"))
    # It must be able to REPLY: an outbox reply is a file write, so a blanket
    # Write deny would have made this desk mute - and a deny cannot escalate.
    # Was `any(x.startswith("Write(") and "outbox" in x ...)`. That asserted a
    # SCOPED rule which, measured 2026-08-06, matches nothing: tool.transcribe's
    # reply was refused twice and only escaped via api.py. A bare Write is what
    # actually satisfies the intent, so test the intent.
    check("fleet desk may write its own outbox (or it cannot answer at all)",
          "Write" in _fs["permissions"]["allow"]
          or any(x.startswith("Write(") and "outbox" in x
                 for x in _fs["permissions"]["allow"]))
    check("fleet desk has no blanket Write deny (would silence it, and deny cannot escalate)",
          "Write" not in _fs["permissions"]["deny"])
    # "fleet desk cannot push or commit" was REMOVED 2026-08-06 on the owner's
    # instruction: "Remember i said maybe i tell session to push?" - a desk he
    # tells to push must be able to. Only the .env fence remains; the brake is
    # now the model asking him in words before anything irreversible (USER.md).
    # Two kinds of deny, and only two. The .env fence is about SECRECY. The
    # answerability denies (sync_permissions.DENY) are about a desk that would
    # otherwise hang forever on a terminal widget nobody can reach - not a
    # capability judgement at all. Anything else here would be re-growing the
    # rails he removed on 2026-08-06.
    _allowed_denies = set()
    for _t in ("AskUserQuestion",):
        _allowed_denies.add(_t)
    check("only the .env fence and the unanswerable-widget denies survive on the fleet desk",
          all("env" in x.lower() or x in _allowed_denies
              for x in _fs["permissions"]["deny"]),
          f"unexpected: {[x for x in _fs['permissions']['deny'] if 'env' not in x.lower() and x not in _allowed_denies]}")

    print("== !reload ==")
    # Until this existed, every watchdog.py edit needed physical access: Python
    # imports at startup, so a running watchdog keeps the old code forever.
    check("!reload is a registered control command", "!reload" in wd.CONTROL_COMMANDS)
    _src = (HERE / "watchdog.py").read_text(encoding="utf-8")
    check("!reload compile-checks before re-exec (never exec into code that cannot start)",
          "compile(f.read_text" in _src and "reload refused" in _src)
    check("!reload releases the lock first (else the replacement exits 3)",
          _src.index("release_lock()   # the replacement") < _src.index("os.execv"))
    check("!reload re-acquires the lock if exec fails",
          "acquire_lock()          # exec failed" in _src)
    # A refusal must NOT kill the bus: it reports and keeps serving.
    sent.clear()
    real_send = wd.api.send_message
    bad = HERE / "_reload_probe.py"
    try:
        bad.write_text("def broken(:\n", encoding="utf-8")   # deliberate syntax error
        try:
            compile(bad.read_text(encoding="utf-8"), str(bad), "exec")
            ok = False
        except SyntaxError:
            ok = True
        check("a syntax error is detectable by the same compile() the guard uses", ok)
    finally:
        bad.unlink(missing_ok=True)
        wd.api.send_message = real_send

    print("== daybook desk ==")
    # User decision 2026-07-31: #daybook gets its OWN session so capturing notes
    # never occupies the orchestrator. Two latent crashes were in the way.
    check("cwd_for('daybook') is <root>\\daybook, not projects\\daybook",
          wd.cwd_for("daybook") == wd.ROOT / "daybook")
    check("cwd_for('tool.x') is <root>\\tools\\x (matched inbox_watch, was broken)",
          wd.cwd_for("tool.whisper") == wd.ROOT / "tools" / "whisper")
    check("cwd_for still right for a component",
          wd.cwd_for("demo-app.app") == wd.ROOT / "projects" / "demo-app" / "app")
    # primary_channel_id used session.split(".",1)[1] -> IndexError on dot-less ids
    _m = {"CID_DAY": T("daybook", "daybook"), "CID_ORCH": T("orchestrator", "orchestrator")}
    check("primary_channel_id survives a dot-less session id",
          wd.primary_channel_id(_m, "daybook") == "CID_DAY")
    check("primary_channel_id unchanged for orchestrator",
          wd.primary_channel_id(_m, "orchestrator") == "CID_ORCH")
    wd_src_db = (HERE / "watchdog.py").read_text(encoding="utf-8")
    check("build_map routes #daybook to the daybook session, not the orchestrator",
          'session = "daybook"' in wd_src_db)
    # A desk with no profile and no skill stub cannot be spawned unattended
    # (spawn-saga fixes 2 and 4).
    check("daybook desk has its own settings.json",
          (HERE.parent.parent / "daybook" / ".claude" / "settings.json").is_file())
    check("daybook desk has the omnius skill stub",
          (HERE.parent.parent / "daybook" / ".claude" / "skills" / "omnius" / "SKILL.md").is_file())

    print("== one run per desk (R3, run model) ==")
    # 2026-07-31: a second session was put on a busy desk and both drained the
    # same inbox. The rule survives the run model because ensure_runner is the
    # SINGLE choke point - serialization cannot drift between callers.
    import os as _pid_os
    box_r = wd.INBOX / "alpha.app"
    box_r.mkdir(parents=True, exist_ok=True)
    (wd.ROOT / "projects" / "alpha" / "app").mkdir(parents=True, exist_ok=True)
    wd.SESSIONS.mkdir(parents=True, exist_ok=True)
    wd.RUNS.mkdir(parents=True, exist_ok=True)
    wd.TURNS.mkdir(parents=True, exist_ok=True)
    wd.RUNNING.clear(); wd._run_backoff.clear(); wd._run_failures.clear()
    for f in list(box_r.glob("*.json")) + list(wd.RUNS.glob("*.json")):
        f.unlink()
    spawned.clear()

    check("empty inbox -> no run started",
          wd.ensure_runner("alpha.app") == "empty" and spawned == [])
    (box_r / "1.json").write_text("{}", encoding="utf-8")
    check("queued mail -> a run starts",
          wd.ensure_runner("alpha.app") == "started" and spawned == ["alpha.app"])
    # the stub starts nothing, so stand the running child up by hand
    wd.RUNNING["alpha.app"] = FakeProc()
    spawned.clear()
    check("a second message queues behind the active run",
          wd.ensure_runner("alpha.app") == "run-in-progress" and spawned == [])
    # the run exits cleanly WITH its envelope drained -> next mail, next run
    wd.RUNNING["alpha.app"]._rc = 0
    (box_r / "1.json").unlink()
    (box_r / "2.json").write_text("{}", encoding="utf-8")
    check("after a clean run, new mail starts the next run",
          wd.ensure_runner("alpha.app") == "started" and spawned == ["alpha.app"])
    wd.RUNNING.pop("alpha.app", None)

    # lease adoption: a previous watchdog's run holds the desk via its pid
    wd.write_json_atomic(wd.RUNS / "alpha.app.json",
                         {"session": "alpha.app", "pid": _pid_os.getpid()})
    spawned.clear()
    check("a live lease from a previous watchdog holds the desk (adopted, not doubled)",
          wd.ensure_runner("alpha.app") == "run-in-progress" and spawned == [])
    wd.write_json_atomic(wd.RUNS / "alpha.app.json",
                         {"session": "alpha.app", "pid": 99999997})
    spawned.clear()
    check("a dead lease is cleaned up and the run starts",
          wd.ensure_runner("alpha.app") == "started"
          and not (wd.RUNS / "alpha.app.json").exists() and spawned == ["alpha.app"])

    # interactive guard: a person's terminal mid-turn holds headless runs off
    (wd.TURNS / "alpha.app.busy").write_text("{}", encoding="utf-8")
    claim("alpha.app", pid=_pid_os.getpid())
    spawned.clear()
    check("a terminal mid-turn holds headless runs off the desk",
          wd.ensure_runner("alpha.app") == "terminal-busy" and spawned == [])
    claim("alpha.app", pid=99999996)   # the terminal died without its Stop hook
    check("a busy stamp with a dead terminal is litter - the run proceeds",
          wd.ensure_runner("alpha.app") == "started"
          and not (wd.TURNS / "alpha.app.busy").is_file())

    # ...and the same stamp on a terminal that is very much ALIVE. A desk's
    # process outlives every turn it runs, so a Stop hook that did not fire (Esc
    # mid-turn, a hook timeout, the 2026-08-12 identity drift) leaves a stamp no
    # pid check can invalidate - and the desk is deaf until a human notices.
    # 2026-08-12, the owner: "this is a showstopper, it has to run for weeks".
    _stamp = wd.TURNS / "alpha.app.busy"
    _tsf = wd.turn_silent_for

    def _stale_busy(silent, perms_pending=False, mins_old=30):
        _stamp.write_text(json.dumps({"session": "alpha.app",
                                      "claudeSession": "abc-123"}), encoding="utf-8")
        _old = wd.time.time() - mins_old * 60
        _pid_os.utime(_stamp, (_old, _old))
        claim("alpha.app", pid=_pid_os.getpid())     # HIS session, alive, between turns
        wd.turn_silent_for = lambda stamp: silent
        wd.PERMS.mkdir(parents=True, exist_ok=True)
        (wd.PERMS / "alpha.app.stalled").unlink(missing_ok=True)
        if perms_pending:
            (wd.PERMS / "alpha.app.stalled").write_text("{}", encoding="utf-8")
        wd._run_backoff.clear()
        spawned.clear()
        return wd.ensure_runner("alpha.app")

    try:
        check("a live turn still holds the desk (its conversation is being written)",
              _stale_busy(silent=40.0) == "terminal-busy" and _stamp.is_file())
        check("a stamp whose conversation went silent is released - the desk answers",
              _stale_busy(silent=wd.BUSY_SILENT_SECONDS + 60) == "started"
              and not _stamp.is_file())
        check("...but never while a permission decision is pending (a parked turn is a real turn)",
              _stale_busy(silent=wd.BUSY_SILENT_SECONDS + 60, perms_pending=True) == "terminal-busy"
              and _stamp.is_file())
        check("...and never on a fresh stamp, however quiet (long thinking is not a dead turn)",
              _stale_busy(silent=wd.BUSY_SILENT_SECONDS + 60, mins_old=2) == "terminal-busy"
              and _stamp.is_file())
        check("no evidence either way keeps the desk busy - never guess a turn is over",
              _stale_busy(silent=None) == "terminal-busy" and _stamp.is_file())
    finally:
        wd.turn_silent_for = _tsf
        (wd.PERMS / "alpha.app.stalled").unlink(missing_ok=True)
        _stamp.unlink(missing_ok=True)
    _wsrc_busy = (HERE / "watchdog.py").read_text(encoding="utf-8")
    check("the evidence is the TURN's own conversation, found by the id the stamp records",
          "claudeSession" in _wsrc_busy.split("def turn_silent_for")[1]
                                       .split("def interactive_busy")[0])
    # A pending permission request names its desk; the relay writes exactly that.
    check("a pending permission request is recognised by its session field",
          '"session": session' in (HERE / "permission_relay.py").read_text(encoding="utf-8"))

    (wd.SESSIONS / "alpha.app.json").unlink(missing_ok=True)

    # failure backoff: a crashing run must not become a spawn-per-3s loop
    wd.RUNNING["alpha.app"] = FakeProc(rc=1)
    wd._reap("alpha.app")
    spawned.clear()
    check("a failed run puts the desk in backoff",
          wd.ensure_runner("alpha.app") == "backoff" and spawned == [])
    check("backoff is bounded, not forever",
          wd._run_backoff["alpha.app"] <= wd.time.time() + wd.RUN_BACKOFF_SECONDS + 1)
    # rc 0 but the same oldest envelope still queued = it did not do its job
    wd._run_backoff.clear(); wd._run_failures.clear()
    wd._run_oldest["alpha.app"] = "2.json"
    wd.RUNNING["alpha.app"] = FakeProc(rc=0)
    wd._reap("alpha.app")
    check("an rc-0 run that left its oldest envelope unhandled counts as a failure",
          wd._run_failures.get("alpha.app") == 1)
    # three consecutive failures tell the owner - once, not per failure
    _rbm, _rpc = wd.build_map, wd.primary_channel_id
    wd.build_map = lambda schema: {}
    wd.primary_channel_id = lambda m, s: "C-alpha"
    sent.clear()
    wd._run_failures["alpha.app"] = 2
    wd.RUNNING["alpha.app"] = FakeProc(rc=1)
    wd._reap("alpha.app")
    check("the third consecutive failure alerts the owner",
          len(sent) == 1 and "alpha.app" in sent[0][1] and "!restart" in sent[0][1])
    wd.RUNNING["alpha.app"] = FakeProc(rc=1)
    wd._reap("alpha.app")
    check("the alert does not repeat on the fourth failure", len(sent) == 1)
    wd.build_map, wd.primary_channel_id = _rbm, _rpc

    # kill_session resets the whole ledger - !restart must start clean
    (wd.TURNS / "alpha.app.busy").write_text("{}", encoding="utf-8")
    wd.write_json_atomic(wd.RUNS / "alpha.app.json", {"session": "alpha.app", "pid": 99999917})
    _real_kill_session("alpha.app")
    check("kill_session clears busy stamp, lease, backoff and failure count",
          not (wd.TURNS / "alpha.app.busy").is_file()
          and not (wd.RUNS / "alpha.app.json").exists()
          and "alpha.app" not in wd._run_backoff
          and "alpha.app" not in wd._run_failures)
    for f in box_r.glob("*.json"):
        f.unlink()
    wd._run_backoff.clear(); wd._run_failures.clear(); wd._run_alerted.clear()
    wd._run_oldest.clear()

    # == the desk bridge ========================================================
    # 2026-08-02: measured one-word replies at 31s/86s/87s, nearly all of it a
    # fresh claude booting per message. A bridge keeps ONE warm session in a
    # real window and types the mail into it, so reply time = thinking time.
    # The watchdog's job here is only to get out of its way.
    # == Discord takes an idle desk, even one he left a window on ==============
    # His rule, 2026-08-03: "if for some reason I forgot to close a native CLI
    # window when writing from discord, you close them and then start the
    # bridge". A native session has no claim unless he ran /omnius, so the bus
    # cannot see it - psutil's cwd is the only discriminator, and WMI has none.
    print("== native-session takeover ==")
    # 2026-08-03, the bug that must never return: the watchdog ASKED whether to
    # take a desk, he had not answered, and 15 minutes of window-quiet later it
    # took it anyway - killing a session he was working in. Asking and then
    # acting is worse than either policy alone. Only his "ok" closes a window.
    _nat = []
    _real_close = wd.close_native_sessions
    _real_native = wd.native_sessions
    _real_start = wd.start_run
    wd.native_sessions = lambda s: [4242]
    wd.close_native_sessions = lambda s, force=False: (_nat.append((s, force)), [4242])[1]
    wd.start_run = lambda s, **kw: (spawned.append(s), True)[1]
    _tbox2 = wd.INBOX / "takeover.desk"
    _tbox2.mkdir(parents=True, exist_ok=True)
    (_tbox2 / "1.json").write_text("{}", encoding="utf-8")
    wd.RUNNING.clear(); wd._run_backoff.clear(); spawned.clear(); sent.clear()
    wd.TAKEOVER = SAND / "takeover"
    wd.TAKEOVER.mkdir(parents=True, exist_ok=True)
    (wd.TAKEOVER / "takeover.desk.json").unlink(missing_ok=True)
    _rbm3, _rpc3 = wd.build_map, wd.primary_channel_id
    wd.build_map = lambda schema: {}
    wd.primary_channel_id = lambda m, s: "C-take"
    _riu1 = wd.native_in_use
    wd.native_in_use = lambda s: 99999.0          # ancient: the old code would kill
    wd._native_notified.clear()
    try:
        _r = wd.ensure_runner("takeover.desk")
        check("a desk with ANY window of his is never taken automatically",
              _r == "owner-at-the-desk" and _nat == [] and spawned == [])
        check("...however long that window has been quiet",
              wd.native_in_use("takeover.desk") > wd.NATIVE_IN_USE_SECONDS and _nat == [])
        check("...and he is asked instead", len(sent) == 1 and "`ok`" in sent[-1][1])
        # Asking again while unanswered must not become an excuse to act.
        sent.clear()
        check("re-checking a desk he has not answered still takes nothing",
              wd.ensure_runner("takeover.desk") == "owner-at-the-desk" and _nat == [])
        # His rule: ignoring costs nothing, and the poll loop must not nag -
        # the SAME message is asked about exactly once.
        check("...and does not ask again about the same message", sent == [])
        # But the next thing he writes re-offers, because that is the natural
        # moment: "whenever i write again in discord you ask again".
        (_tbox2 / "2.json").write_text("{}", encoding="utf-8")
        check("a NEW message asks again",
              wd.ensure_runner("takeover.desk") == "owner-at-the-desk"
              and len(sent) == 1 and "`ok`" in sent[-1][1] and _nat == [])
        # Only the answer closes it, and only with force.
        wd.answer_takeover("ok", "takeover.desk")
        check("only his 'ok' closes a window, and it says so explicitly",
              _nat == [("takeover.desk", True)])
    finally:
        wd.close_native_sessions, wd.start_run = _real_close, _real_start
        wd.native_sessions, wd.native_in_use = _real_native, _riu1
        wd.build_map, wd.primary_channel_id = _rbm3, _rpc3
        for f in _tbox2.glob("*.json"):
            f.unlink()
        spawned.clear(); sent.clear(); wd._native_notified.clear()

    print("== takeover question ==")
    wd.TAKEOVER = SAND / "takeover"
    wd.TAKEOVER.mkdir(parents=True, exist_ok=True)
    _tk_box = wd.INBOX / "ask.desk"
    _tk_box.mkdir(parents=True, exist_ok=True)
    (_tk_box / "1.json").write_text("{}", encoding="utf-8")
    _rn, _rc, _riu = wd.native_sessions, wd.close_native_sessions, wd.native_in_use
    _rbm4, _rpc4 = wd.build_map, wd.primary_channel_id
    wd.native_sessions = lambda s: [111]
    wd.close_native_sessions = lambda s, force=False: [111] if force else []
    wd.native_in_use = lambda s: 180.0
    wd.build_map = lambda schema: {}
    wd.primary_channel_id = lambda m, s: "C-ask"
    wd._native_notified.clear(); sent.clear()
    try:
        # Mid-turn is not a question - asking would invite him to kill live work.
        # The stamp needs a LIVE owner to count (turn_busy, 2026-08-11): an
        # orphaned one used to silence the desk forever, so the test has to
        # model a real mid-turn desk, which always has a live session.
        (wd.TURNS / "ask.desk.busy").write_text("{}", encoding="utf-8")
        _sa_ask = wd.session_alive
        wd.session_alive = lambda s: True
        wd.report_native_in_use("ask.desk")
        wd.session_alive = _sa_ask
        check("a desk mid-turn is reported as busy, with no question asked",
              "working right now" in sent[-1][1] and not wd.takeover_pending("ask.desk"))
        (wd.TURNS / "ask.desk.busy").unlink()
        wd._native_notified.clear(); sent.clear()

        wd.report_native_in_use("ask.desk")
        check("an idle desk at the keyboard gets a real question",
              "`ok`" in sent[-1][1] and "`no`" in sent[-1][1] and "waiting" in sent[-1][1])
        check("...and the question is recorded so an answer can match it",
              bool(wd.takeover_pending("ask.desk")))

        check("'no' leaves the desk alone and says the mail keeps waiting",
              "Leaving" in (wd.answer_takeover("no", "ask.desk") or ""))
        # "no" answers THIS message only - no mute window. A clock has been the
        # wrong shape here twice; his rule is that the next message asks again.
        sent.clear()
        check("...and does not re-ask about the message he just declined",
              wd.ensure_runner("ask.desk") == "owner-at-the-desk" and sent == [])
        (_tk_box / "2.json").write_text("{}", encoding="utf-8")
        check("...but the NEXT message he sends asks again, with no timer",
              wd.ensure_runner("ask.desk") == "owner-at-the-desk"
              and len(sent) == 1 and "`ok`" in sent[-1][1])
        check("no decline timer survives anywhere",
              "TAKEOVER_DECLINED_SECONDS" not in _wsrc)
        check("'ok' closes the window, by force, and says which",
              "Took" in (wd.answer_takeover("ok", "ask.desk") or ""))
        check("ordinary chat is never swallowed as an answer",
              wd.answer_takeover("what is the build status?", "ask.desk") is None)
        check("a desk that never asked ignores ok entirely",
              wd.answer_takeover("ok", "never-asked.desk") is None)
        _hm = _wsrc[_wsrc.index("def handle_message"):]
        check("the answer is intercepted BEFORE the message becomes mail",
              _hm.index("answer_takeover") < _hm.index("write_envelope"))
    finally:
        wd.native_sessions, wd.close_native_sessions, wd.native_in_use = _rn, _rc, _riu
        wd.build_map, wd.primary_channel_id = _rbm4, _rpc4
        for f in _tk_box.glob("*.json"):
            f.unlink()
        wd._native_notified.clear(); sent.clear()

    # Recency is the whole discriminator, so pin how it is measured.
    check("in-use is judged by the conversation file's mtime, not a stamp",
          "history_dir_for" in _wsrc.split("def native_in_use")[1].split("def close_native")[0])
    check("close_native_sessions refuses unless he explicitly said so",
          "only he can close them" in _wsrc)
    check("...and the auto-close-after-quiet path is GONE, not merely tightened",
          "NOT closing" not in _wsrc)
    _nsrc = _wsrc if "def native_sessions" in _wsrc else \
        (_rr2 / "tools" / "discord" / "watchdog.py").read_text(encoding="utf-8")
    check("native sessions are found by CWD (the only reliable discriminator)",
          "p.cwd()" in _nsrc and "cwd_for(session)" in _nsrc)
    check("...and the bridge's own claude is never mistaken for one",
          "children(recursive=True)" in _nsrc)

    print("== bridge ownership ==")
    wd.BRIDGES = SAND / "bridges"
    wd.BRIDGES.mkdir(parents=True, exist_ok=True)
    _bbox = wd.INBOX / "bridged.desk"
    _bbox.mkdir(parents=True, exist_ok=True)
    for f in _bbox.glob("*.json"):
        f.unlink()
    wd.RUNNING.clear(); wd._run_backoff.clear(); wd._run_failures.clear()
    spawned.clear()

    check("no bridge file -> not bridged", not wd.bridge_active("bridged.desk"))
    (wd.BRIDGES / "bridged.desk.json").write_text(
        json.dumps({"session": "bridged.desk", "pid": _pid_os.getpid()}), encoding="utf-8")
    check("a live bridge owns the desk", wd.bridge_active("bridged.desk"))
    (_bbox / "1.json").write_text("{}", encoding="utf-8")
    check("mail on a bridged desk starts NO run (the warm session gets it)",
          wd.ensure_runner("bridged.desk") == "bridge-owns-desk" and spawned == [])
    # The fallback that makes this safe to try: a dead bridge is not a deaf desk.
    (wd.BRIDGES / "bridged.desk.json").write_text(
        json.dumps({"session": "bridged.desk", "pid": 99999995}), encoding="utf-8")
    check("a dead bridge is cleaned up and the desk falls back to a headless run",
          wd.ensure_runner("bridged.desk") == "started"
          and not (wd.BRIDGES / "bridged.desk.json").exists())
    for f in _bbox.glob("*.json"):
        f.unlink()
    wd.RUNNING.clear(); spawned.clear()

    # The bridge itself: guards proven without a pty, since a wrong nudge lands
    # inside a sentence the owner is typing.
    _rr = HERE.parent.parent          # real workspace root (real_root is bound later)
    import time as _bt
    sys.path.insert(0, str(_rr / "tools" / "bridge"))
    import desk_bridge as _db
    _db.INBOX, _db.TURNS = wd.INBOX, wd.TURNS
    _b = _db.Bridge.__new__(_db.Bridge)
    _b.session, _b.box = "bridged.desk", wd.INBOX / "bridged.desk"
    _b.last_human = _b.last_nudge = 0.0
    _b.nudge_took = False
    _b.started_at = _bt.time() - 60          # booted a minute ago
    wd.TURNS.mkdir(parents=True, exist_ok=True)
    check("bridge: empty inbox -> no nudge", _b.may_nudge()[0] is False)
    (_b.box / "1.json").write_text("{}", encoding="utf-8")
    check("bridge: mail + quiet desk -> nudge", _b.may_nudge()[0] is True)
    _b.last_human = _bt.time()
    check("bridge: NEVER types while the owner is typing", _b.may_nudge()[0] is False)
    _b.last_human = 0.0
    (wd.TURNS / "bridged.desk.busy").write_text("{}", encoding="utf-8")
    check("bridge: never queues a prompt behind a running turn",
          _b.may_nudge()[0] is False)
    (wd.TURNS / "bridged.desk.busy").unlink()
    _b.last_nudge = _bt.time()
    check("bridge: cools down instead of hammering a session that has not started",
          _b.may_nudge()[0] is False)
    # Measured on the first real takeover (2026-08-03): the bridge nudged the
    # same second it launched claude, into a session that did not exist, then
    # served the full 20s cooldown for that miss - 25 wasted seconds.
    _b.started_at = _bt.time()
    _b.last_nudge = 0.0
    check("bridge: does not nudge a session that is still booting",
          _b.may_nudge()[0] is False and "booting" in _b.may_nudge()[1])
    _b.started_at = _bt.time() - 60
    _b.last_nudge = _bt.time() - 6; _b.nudge_took = False
    check("bridge: retries a nudge that never started a turn (4s, not 20s)",
          _b.may_nudge()[0] is True)
    _b.last_nudge = _bt.time() - 6; _b.nudge_took = True
    check("bridge: waits the long floor only when the nudge actually landed",
          _b.may_nudge()[0] is False)
    _b.nudge_took = False; _b.last_nudge = 0.0
    for f in _b.box.glob("*.json"):
        f.unlink()
    # The bridge must obey the desk's RESUME POLICY, not just its own instinct
    # to stay warm. Until 2026-08-16 it always passed --continue, so a takeover
    # moved the orchestrator - the one desk set `resume: "fresh"` because its
    # dev transcript dwarfs the chat - onto a multi-MB conversation to answer a
    # Discord line. He felt it immediately: "after the takeover your responses
    # were really slow". start_run had honoured this since 2026-08-01.
    _real_deskcfg, _real_hashist = wd.desk_config, wd.has_history
    try:
        wd.desk_config = lambda s: {"model": "opus", "effort": "xhigh",
                                    "resume": "fresh" if s == "orchestrator" else "transcript"}
        wd.has_history = lambda cwd: True
        _argv_fresh = _db.claude_argv("orchestrator", wd.ROOT)
        _argv_warm = _db.claude_argv("demo-app.app", wd.ROOT / "projects" / "demo-app" / "app")
        check("bridge: a `resume: fresh` desk is NOT resumed - no --continue",
              "--continue" not in _argv_fresh, f"got {_argv_fresh}")
        check("bridge: ...while every other desk still resumes its own conversation",
              "--continue" in _argv_warm)
    finally:
        wd.desk_config, wd.has_history = _real_deskcfg, _real_hashist
    # Tab naming. Claude Code renames every tab "Claude Code" via ESC ]0;...,
    # so six open desks were indistinguishable (owner, 2026-08-02). The bridge
    # is in the byte stream, so it strips the app's title and re-asserts the
    # desk id - and must not disturb one other byte of the TUI.
    _bt2 = _db.Bridge.__new__(_db.Bridge)
    _bt2.session, _bt2.title_seq, _bt2._tail = "orchestrator", "\x1b]0;orchestrator\x07", ""
    _rendered = _bt2._rename("\x1b[?25l\x1b]0;✳ Claude Code\x07\x1b[H hello")
    check("tab naming: the app's own title is stripped",
          "Claude Code" not in _rendered)
    check("tab naming: the desk id is asserted instead",
          "\x1b]0;orchestrator\x07" in _rendered)
    check("tab naming: every other TUI byte passes through untouched",
          "\x1b[?25l" in _rendered and "\x1b[H hello" in _rendered)
    _bt2._tail = ""
    _p1 = _bt2._rename("text\x1b]0;✳ Clau")      # sequence split across two reads
    _p2 = _bt2._rename("de Code\x07more")
    check("tab naming: a split title sequence is held back, never shown as garbage",
          "Clau" not in _p1 and "Claude Code" not in (_p1 + _p2)
          and "text" in _p1 and "more" in _p2)

    _db_src = (_rr / "tools" / "bridge" / "desk_bridge.py").read_text(encoding="utf-8")
    check("the bridge types ONLY /omnius - it is a keyboard, not a brain",
          'NUDGE = "/omnius"' in _db_src)
    # Assert on CODE, not prose: this file's docstrings name outboxes and
    # heartbeats precisely to explain why the bridge has neither, and a check
    # that forbids the explanation is a check that punishes documentation
    # (fourth time this trap has been walked into here).
    check("...and owns no reply path of its own (replies stay session->outbox)",
          "OUTBOX" not in _db_src)
    # Parsed, not grepped: the docstring says "while" and "heartbeat" precisely
    # to explain their absence. Only the AST can tell prose from code.
    import ast as _ast2
    _tree = _ast2.parse(_db_src)
    _fns = {n.name: n for n in _ast2.walk(_tree)
            if isinstance(n, (_ast2.FunctionDef, _ast2.AsyncFunctionDef))}
    check("the bridge announces ONCE by pid - no loop, so nothing can lie for it",
          "announce" in _fns and "withdraw" in _fns
          and not [n for n in _ast2.walk(_fns["announce"])
                   if isinstance(n, (_ast2.While, _ast2.For))]
          and "os.getpid()" in _ast2.get_source_segment(_db_src, _fns["announce"]))

    # == --settings routing, and the window loop it caused =====================
    # 2026-08-02, found on the owner's first reboot: daybook got a fresh window
    # every 150s all morning. Cause chain worth keeping whole - ONE wrong path
    # produced an infinite spawn: daybook is one level down, so cwd.parent was
    # the ROOT, so it was handed the ROOT settings file, whose hook commands are
    # relative to ${CLAUDE_PROJECT_DIR} = the DESK folder -> every hook path
    # missing -> a failing UserPromptSubmit hook BLOCKS the prompt -> the desk
    # cannot run /omnius -> never claims -> the tab lease expires -> open another.
    # == permissions: the check-in must never need approval ====================
    # 2026-08-02, first clean reboot: the orchestrator desk came up and stalled
    # asking permission to run its OWN check-in. The allow list had
    # Bash(python:*) but Claude Code ran it through the PowerShell tool, and a
    # permission rule is per-tool. Escalation cannot rescue this one BY DESIGN:
    # the relay only fires for a bus-connected desk, and the gated command is
    # the one that connects it. So the allow list is the only possible fix, and
    # a missing twin means every desk stalls on boot with nobody to click.
    _rr2 = HERE.parent.parent
    # == hook paths must be absolute ===========================================
    # THREE desk failures in one morning, all the same root: a depth-relative
    # ${CLAUDE_PROJECT_DIR}/../..&/ hook path. It cannot be made correct,
    # because --add-dir <root> loads the ROOT settings file into sessions at
    # other depths, so one spelling is wrong for somebody. And the failure is
    # total: python exits 2, a failing UserPromptSubmit hook BLOCKS the prompt,
    # and the desk cannot run /omnius at all - it just looks hung.
    # == a pid is not an identity ==============================================
    # 2026-08-02, Discord dead for hours after a reboot: the watchdog's lock
    # file survived holding pid 5568, Windows had reassigned that number to
    # AsusOptimizationStartupTask.exe, pid_alive() said True, and every start
    # refused with "another watchdog is already running". Boot time cannot fix
    # it either - Fast Startup leaves LastBootUpTime reporting the PREVIOUS
    # cold boot, so "written before the last boot?" is unanswerable. Identity is.
    print("== pid identity ==")
    wd.process_image = _real_process_image          # identity itself under test
    check("process_image names the exe behind a pid",
          (wd.process_image(_os0.getpid()) or "").startswith("python"))
    check("a live pid of the WRONG kind is not accepted (the reused-pid case)",
          wd.pid_alive(_os0.getpid(), expect="claude") is False)
    check("...while the right kind still is",
          wd.pid_alive(_os0.getpid(), expect="python") is True)
    check("a dead pid stays dead whatever we expect",
          wd.pid_alive(99999997, expect="python") is False)
    check("no expectation keeps the old behaviour",
          wd.pid_alive(_os0.getpid()) is True)
    wd.process_image = lambda pid: None             # back to the simulation
    # The lock is the one that took Discord down, so pin it by source.
    _al = _wsrc[_wsrc.index("def acquire_lock"):]
    _al = _al[:_al.index("\ndef ", 10)]
    check("the watchdog lock demands a python process, not just a live pid",
          'expect="python"' in _al)
    for _fn, _want in (("def bridge_active", "python"), ("def stale_claims", "claude"),
                       ("def session_alive", "claude")):
        _body = _wsrc[_wsrc.index(_fn):]
        _body = _body[:_body.index("\ndef ", 10)]
        check(f"{_fn.split()[1]} validates the process kind ({_want})",
              f'expect="{_want}"' in _body)

    print("== hook paths ==")
    import subprocess as _sp3
    import fix_hook_paths as _fhp
    _fix = _rr2 / "tools" / "discord" / "fix_hook_paths.py"
    check("the hook-path repair tool exists", _fix.is_file())
    _chk = _sp3.run([sys.executable, str(_fix), "--check"], capture_output=True, text=True)
    check("every desk is wired to THIS machine, and no tracked file holds a path",
          _chk.returncode == 0, (_chk.stdout + _chk.stderr).strip()[:400])
    # THE leak that made the public repo unusable (2026-08-14): six tracked
    # settings.json files carried one machine's home directory, so every prompt
    # typed in a clone died with "UserPromptSubmit operation blocked by hook".
    # Hooks are machine state and belong in the gitignored settings.local.json;
    # a hooks block back in a tracked file is that bug returning, so it fails
    # here rather than on a stranger's PC.
    _tracked_hooks = [_fhp._rel(p) for p in _fhp.tracked_settings()
                      if "hooks" in _fhp.read_json(p)]
    check("no tracked settings.json carries hooks (they are this machine's paths)",
          not _tracked_hooks, f"would ship a path: {_tracked_hooks}")
    check("...and the local file that does carry them is gitignored",
          "**/.claude/settings.local.json" in
          (_rr2 / ".gitignore").read_text(encoding="utf-8"))
    check("...and never travels in a release zip either",
          "--exclude=settings.local.json" in (_rr2 / "pack.ps1").read_text(encoding="utf-8"))
    # Depth-relative paths were abandoned 2026-08-02 (one settings file, many
    # session depths). Absolute is the only spelling that works - which is
    # exactly why it must be generated, never committed.
    check("no settings file has gone back to a depth-relative hook path",
          not any("${CLAUDE_PROJECT_DIR}" in p.read_text(encoding="utf-8")
                  for p in _fhp.tracked_settings()))
    check("the hooks written are all three (permission relay + both turn stamps)",
          set(_fhp.hooks_block()) == {"PermissionRequest", "Stop", "UserPromptSubmit"})
    check("...and every one points at a script this checkout actually has",
          not _fhp.missing_scripts(), f"missing: {_fhp.missing_scripts()}")
    # A desk is a CWD, not a settings file: the watchdog starts a component in
    # projects\<p>\<c> and passes the PROJECT's settings.json with --settings,
    # so hooks have to come from the component's own folder. Verified live
    # 2026-08-14 that a cwd-local settings.local.json fires even then.
    _dd = [str(d) for d in _fhp.desk_dirs()]
    check("the root desk gets hooks", str(_fhp.ROOT) in _dd)
    check("...and daybook", str(_fhp.ROOT / "daybook") in _dd)
    check("...and every tool that is a desk (has its own .claude)",
          all(str(_fhp.ROOT / "tools" / t) in _dd
              for t in ("email", "fleet", "transcribe")
              if (_fhp.ROOT / "tools" / t / ".claude").is_dir()))
    check("...and templates\\ never does - a skeleton is not a desk",
          not any("templates" in d for d in _dd))
    _as = (_rr2 / "tools" / "discord" / "autostart.ps1").read_text(encoding="utf-8")
    check("autostart repairs hook paths before starting anything (workspaces move)",
          "fix_hook_paths.py" in _as)
    _fo = (_rr2 / "tools" / "orchestrator" / "fleet_ops.py").read_text(encoding="utf-8")
    check("a newly stamped project gets its hook paths fixed too",
          "fix_hook_paths.py" in _fo)

    print("== permission twins (Bash <-> PowerShell) ==")
    for _sf in (".claude/settings.json", "templates/project/.claude/settings.json",
                "daybook/.claude/settings.json", "tools/fleet/.claude/settings.json"):
        _p = _rr2 / _sf
        if not _p.is_file():
            continue
        _allow = json.loads(_p.read_text(encoding="utf-8"))["permissions"]["allow"]
        _missing = [r for r in _allow if r.startswith("Bash(")
                    and "PowerShell(" + r[5:] not in _allow]
        check(f"{_sf}: every Bash rule has a PowerShell twin", _missing == [],
              f"missing twins for {_missing}")
        # The requirement is that `python ...\inbox_watch.py --once` never
        # prompts, not that it is spelled one particular way. A bare
        # "PowerShell" grant (2026-08-06, owner's call: "auto accept always when
        # using discord/bridge") covers it strictly more widely than the scoped
        # rule did, so accept either rather than pinning the spelling.
        check(f"{_sf}: the desk check-in itself is pre-approved (or nothing boots)",
              "PowerShell" in _allow
              or any(r in ("PowerShell(python:*)", "PowerShell(python C:*)")
                     for r in _allow))

    # == acking mail must not need permission ==================================
    # 2026-08-02: sessions deleted handled envelopes with `Remove-Item <path>`,
    # which no sane allow-list covers, so EVERY Discord message raised a
    # permission prompt. The owner answered four in #alerts and gave up - which
    # is what "the alerts thing doesn't work" actually was. Going through the
    # bus tool makes it `python ...`, already allowed.
    print("== --ack (envelope deletion without a permission prompt) ==")
    import importlib as _il2
    import subprocess as _sp2
    _abox = wd.INBOX / "acktest.desk"
    _abox.mkdir(parents=True, exist_ok=True)
    (_abox / "e1.json").write_text("{}", encoding="utf-8")
    (_abox / "e2.json").write_text("{}", encoding="utf-8")
    _iwpy = str(_rr / "tools" / "discord" / "inbox_watch.py")

    # The real inbox_watch resolves ROOT from its own location, so exercise the
    # pure function against the sandbox instead of the child process.
    _iwmod = _il2.import_module("inbox_watch")
    _iwmod.INBOX = wd.INBOX
    import io as _io3, contextlib as _ctx3
    def _ack_local(*ids):
        buf = _io3.StringIO()
        with _ctx3.redirect_stdout(buf):
            _iwmod.ack("acktest.desk", list(ids))
        return json.loads(buf.getvalue())

    _r = _ack_local("e1")
    check("--ack deletes a handled envelope",
          _r["deleted"] == ["e1.json"] and not (_abox / "e1.json").exists())
    _r = _ack_local("e1")
    check("--ack on an already-handled envelope is not an error (idempotent)",
          _r["notFound"] == ["e1.json"] and _r["refused"] == [])
    _r = _ack_local("../../../.env")
    check("--ack cannot escape this desk's own inbox",
          _r["deleted"] == [] and (_rr / ".env").exists() if (_rr / ".env").exists()
          else _r["deleted"] == [])
    check("...and the sibling envelope is untouched", (_abox / "e2.json").exists())
    (_abox / "e2.json").unlink()
    # A desk must claim the moment its window opens. An unclaimed desk is
    # invisible to !status AND has the permission relay switched off
    # (bus_connected reads the claim), so it prompts at the local screen
    # instead of #alerts - found on the owner's first clean wakeup, 2026-08-02.
    check("check-in accepts a --pid override (the bridge knows its claude pid)",
          _iwmod.resolve_session_pid(424242) == 424242)
    # The same switch, one level subtler, and it hid for as long as tool desks
    # have existed: the desk DOES claim, but tool.* hardcoded discordChannel to
    # None, and bus_connected() reads it as "nobody is driving this remotely"
    # and returns before asking. 2026-08-06, tool.transcribe stopped on a local
    # dialog in a bridge window - "Discord never asked for permission".
    # Resolved from schema.json, because the mapping is NOT mechanical.
    check("a tool desk resolves its channel, so the permission relay stays armed",
          _iwmod._schema_channel("tool.transcribe") == "transcribe")
    check("...from the schema, not by guessing (tool.fleet is #fleet-status)",
          _iwmod._schema_channel("tool.fleet") == "fleet-status")
    check("...and a tool with no schema entry falls back to its name, never None",
          _iwmod._schema_channel("tool.nosuch") is None)
    _iwsrc = (_rr / "tools" / "discord" / "inbox_watch.py").read_text(encoding="utf-8")
    check("...the tool branch never hands bus_connected a falsy channel",
          "_schema_channel(sid) or tool" in _iwsrc)
    _relsrc = (_rr / "tools" / "discord" / "permission_relay.py").read_text(encoding="utf-8")
    check("...which is the value the relay actually gates on",
          'claim.get("discordChannel")' in _relsrc)
    _dbsrc2 = (_rr / "tools" / "bridge" / "desk_bridge.py").read_text(encoding="utf-8")
    check("the bridge claims its desk at startup, not on first message",
          "def claim_desk" in _dbsrc2 and "claim_desk" in
          _dbsrc2.split("def run(self)")[1][:400])
    check("...via inbox_watch, never by hand-writing the claim",
          "inbox_watch.py" in _dbsrc2.split("def claim_desk")[1].split("def run")[0])
    check("...and without spending a model turn (no nudge involved)",
          "--once" in _dbsrc2.split("def claim_desk")[1].split("def run")[0])

    _sk_ack = (_rr / ".claude" / "skills" / "omnius" / "SKILL.md").read_text(encoding="utf-8")
    check("/omnius tells desks to use --ack, and forbids shell deletes",
          "--ack" in _sk_ack and "Remove-Item" in _sk_ack)
    check("/omnius forbids the `cd <root>; python ...` form that matches no rule",
          "never prefix a command with `cd`" in _sk_ack.lower())

    # The answer window has to fit a human with a phone, not a script.
    _relay = (_rr / "tools" / "discord" / "permission_relay.py").read_text(encoding="utf-8")
    check("relay waits long enough for a phone (>= 10 min)",
          "DEFAULT_WAIT = 600" in _relay)
    # Asked of the WRITER, not of a settings file: hooks are generated per
    # machine now (2026-08-14), so the timeout is a property of the definition
    # every desk gets, not of a file that might be a stale copy.
    import fix_hook_paths as _fhp3
    _to = [h.get("timeout") for b in _fhp3.hooks_block()["PermissionRequest"]
           for h in b["hooks"]]
    check("the hook budget exceeds the relay wait (else the ask is capped)",
          all(t and t > 600 for t in _to), f"timeouts={_to}")
    check("a late ok/no in #alerts explains itself instead of 'nobody listens here'",
          "already timed out and fell back" in
          (_rr / "tools" / "discord" / "watchdog.py").read_text(encoding="utf-8"))

    # == a stuck turn must not swallow the mail silently ======================
    # 2026-08-02, live: two desks sat mid-turn for 54 MINUTES holding the
    # owner's "ping XD", frozen on permission dialogs. The busy stamp tells the
    # watchdog "no run" and the bridge "no nudge" - each right alone, deadlock
    # together, and every surface reported the desk as healthily busy.
    print("== stuck turns ==")
    wd.TURNS.mkdir(parents=True, exist_ok=True)
    _sbox = wd.INBOX / "stuck.desk"
    _sbox.mkdir(parents=True, exist_ok=True)
    _sbusy = wd.TURNS / "stuck.desk.busy"
    _sbusy.write_text("{}", encoding="utf-8")
    claim("stuck.desk", pid=_os0.getpid())
    _old = _bt.time() - (wd.STUCK_TURN_SECONDS + 120)
    _os0.utime(_sbusy, (_old, _old))
    wd._stuck_notified.clear(); sent.clear()

    _riu2 = wd.native_in_use
    # This test process stands in for a live claude on the desk: keep the
    # liveness half of pid_alive, drop the exe-name half it cannot satisfy.
    _rpa2 = wd.pid_alive
    wd.pid_alive = lambda pid, expect=None: _rpa2(pid)
    wd.native_in_use = lambda s: 9999.0            # silent for ages = really stuck
    check("a long turn with an EMPTY inbox is left alone (real work takes time)",
          wd.interactive_busy("stuck.desk") is True and sent == [])
    (_sbox / "1.json").write_text(json.dumps({"id": "1", "channelId": "C-s"}), encoding="utf-8")
    _rbm2, _rpc2 = wd.build_map, wd.primary_channel_id
    wd.build_map = lambda schema: {}
    wd.primary_channel_id = lambda m, s: "C-stuck"
    try:
        _busy = wd.interactive_busy("stuck.desk")
        check("a long turn WITH waiting mail is announced to the owner",
              len(sent) == 1 and "mid-turn" in sent[-1][1])
        check("...naming the EVIDENCE and the one-word fix",
              "written nothing" in sent[-1][1] and "!restart" in sent[-1][1])
        # The tempting fix is the dangerous one: clearing the stamp lets the
        # bridge nudge, and a nudge is KEYSTROKES - "/omnius\r" typed into a
        # desk frozen on a permission dialog would answer that dialog.
        check("the stamp is NOT auto-cleared (a nudge would answer the dialog)",
              _busy is True and _sbusy.is_file())
        wd.interactive_busy("stuck.desk")
        check("it does not repeat inside the quiet hour", len(sent) == 1)
        # 2026-08-03: it fired while he was 11 minutes into real work, his
        # session having written 36 seconds earlier, and asserted a permission
        # dialog that did not exist. A long turn and a frozen turn look
        # identical on the clock; on disk they do not - a working session
        # writes continuously, a stopped one writes nothing.
        sent.clear(); wd._stuck_notified.clear()
        wd.native_in_use = lambda s: 36.0          # actively writing
        check("a LONG turn that is still writing is not called stuck",
              wd.interactive_busy("stuck.desk") is True and sent == [])
        wd.native_in_use = lambda s: 9999.0
        wd._stuck_notified.clear()
        wd.interactive_busy("stuck.desk")
        check("...but one that has gone silent still is",
              len(sent) == 1 and "written nothing" in sent[-1][1])
        check("...and it no longer asserts a dialog that does not exist",
              "most likely frozen" not in sent[-1][1])
    finally:
        wd.native_in_use = _riu2
        wd.pid_alive = _rpa2
        wd.build_map, wd.primary_channel_id = _rbm2, _rpc2

    # THE TWO-HOUR DEAF DESK, 2026-08-03 (real pid_alive from here down).
    # His "Hola" reached a warm session at 19:55; it answered two permission
    # asks from Discord and died at a third. The stamp outlived the process,
    # and because "older than STUCK_TURN_SECONDS with mail waiting" was tested
    # BEFORE liveness, the desk called itself mid-turn over a corpse until the
    # 2-hour orphan window expired. Nothing was answered. Liveness must come
    # first: classification of a dead session is meaningless.
    def _stale_stamp(age=None):
        _sbusy.write_text("{}", encoding="utf-8")
        t = _bt.time() - (wd.STUCK_TURN_SECONDS + 120 if age is None else age)
        _os0.utime(_sbusy, (t, t))
    (_sbox / "1.json").write_text(json.dumps({"id": "1", "channelId": "C-s"}), encoding="utf-8")
    _stale_stamp()
    claim("stuck.desk", pid=99999997)              # nothing is running there
    check("a busy stamp whose session is DEAD is litter, not a stuck turn",
          wd.interactive_busy("stuck.desk") is False)
    check("...and it is dropped in that same pass, not two hours later",
          not _sbusy.is_file())
    _stale_stamp()
    # Identity, not merely liveness. The suite simulates process_image as
    # unreadable everywhere else (so expect= fails open); restore the real one
    # for exactly this check, which is about a pid Windows handed to something
    # else entirely - the failure that cost a reboot on 2026-08-02.
    wd.process_image = _real_process_image
    claim("stuck.desk", pid=_os0.getpid())         # alive, but python.exe, not claude.exe
    check("a RECYCLED pid running some other exe cannot hold a desk busy",
          wd.interactive_busy("stuck.desk") is False)
    wd.process_image = lambda pid: None            # back to the simulation
    _stale_stamp(wd.BUSY_ORPHAN_SECONDS + 60)
    (wd.SESSIONS / "stuck.desk.json").unlink(missing_ok=True)
    check("with no claim at all, an ancient stamp is still litter",
          wd.interactive_busy("stuck.desk") is False)
    _sbusy.write_text("{}", encoding="utf-8")      # fresh, unvalidatable
    check("...but a RECENT unvalidatable stamp is still respected",
          wd.interactive_busy("stuck.desk") is True)
    for f in _sbox.glob("*.json"):
        f.unlink()
    _sbusy.unlink(missing_ok=True)
    (wd.SESSIONS / "stuck.desk.json").unlink(missing_ok=True)
    wd._stuck_notified.clear(); sent.clear()

    # ...which makes !restart the escape hatch, so it must clear the BRIDGE too:
    # a surviving bridge keeps bridge_active() true and the desk stays
    # deadlocked by its own rescue.
    _wsrc = (_rr / "tools" / "discord" / "watchdog.py").read_text(encoding="utf-8")
    _kb = _wsrc[_wsrc.index("def kill_session"):]
    _kb = _kb[:_kb.index("\ndef ", 10)]
    check("kill_session also stops the desk bridge and drops its presence file",
          "BRIDGES" in _kb and "unlink" in _kb)
    # He asked outright, 2026-08-03: "can !restart really kill the native CLI?"
    # It can - the claim holds a native session's pid once it has run /omnius.
    # That is correct for an explicit command, but it must never be a surprise,
    # least of all from his sofa with no way to look at the screen.
    check("!restart names it when the thing it killed was HIS OWN window",
          "YOUR OWN window" in _kb and "native_sessions(session)" in _kb)
    check("...and says how recently he was using it",
          "native_in_use(session) if his" in _kb)

    print("== --settings routing ==")
    check("a project component gets its PROJECT settings (they do not inherit)",
          wd.project_settings("demo-app.app", wd.cwd_for("demo-app.app"))
          == wd.ROOT / "projects" / "demo-app" / ".claude" / "settings.json"
          if (wd.ROOT / "projects" / "demo-app" / ".claude" / "settings.json").is_file()
          else wd.project_settings("demo-app.app", wd.cwd_for("demo-app.app")) is None)
    check("daybook gets NO --settings (its own cwd settings already apply)",
          wd.project_settings("daybook", wd.cwd_for("daybook")) is None)
    check("a tool desk gets NO --settings",
          wd.project_settings("tool.fleet", wd.cwd_for("tool.fleet")) is None)
    # The second wrong version pointed the orchestrator at the OWNER'S personal
    # ~\.claude\settings.json, because the root's parent is his home directory.
    check("the orchestrator gets NO --settings, and never the owner's personal one",
          wd.project_settings("orchestrator", wd.cwd_for("orchestrator")) is None)

    print("== a desk that cannot boot must not spawn windows forever ==")
    wd.RUNNING.clear(); wd._run_failures.clear(); wd._run_backoff.clear(); wd._run_alerted.clear()
    wd.RUNS.mkdir(parents=True, exist_ok=True)
    _stale_tab = wd.RUNS / "loopy.desk.json"
    _stale_tab.write_text(json.dumps({"session": "loopy.desk", "mode": "terminal",
                                      "startedAt": "2020-01-01T00:00:00Z"}), encoding="utf-8")
    (wd.SESSIONS / "loopy.desk.json").unlink(missing_ok=True)
    check("an expired tab lease with no claim is a FAILURE, not a free retry",
          wd.run_active("loopy.desk") is False
          and wd._run_failures.get("loopy.desk") == 1
          and wd._run_backoff.get("loopy.desk", 0) > _bt.time())
    _lbox = wd.INBOX / "loopy.desk"
    _lbox.mkdir(parents=True, exist_ok=True)
    (_lbox / "1.json").write_text("{}", encoding="utf-8")
    spawned.clear()
    check("...so mail on that desk waits in backoff instead of opening window #2",
          wd.ensure_runner("loopy.desk") == "backoff" and spawned == [])
    for f in _lbox.glob("*.json"):
        f.unlink()
    wd._run_failures.clear(); wd._run_backoff.clear(); wd._run_alerted.clear()

    # == only HIS mail earns a window ==========================================
    # 2026-08-03: he was working in another project when an orchestrator window
    # opened by itself. Three heartbeat envelopes had queued about a stale claim
    # nobody pruned, and the moment no session of his was detected they opened a
    # desk. "Boot opens nothing" was arrived at from the other direction.
    print("== system mail never opens a window ==")
    _ombox = wd.INBOX / "sysmail.desk"
    _ombox.mkdir(parents=True, exist_ok=True)
    for f in _ombox.glob("*.json"):
        f.unlink()
    (_ombox / "heartbeat-1.json").write_text(json.dumps({"from": "heartbeat"}), encoding="utf-8")
    check("a heartbeat alone is not human mail", wd.human_mail_waiting("sysmail.desk") is False)
    (_ombox / "sched-1.json").write_text(json.dumps({"from": "schedule"}), encoding="utf-8")
    check("nor is a scheduled job", wd.human_mail_waiting("sysmail.desk") is False)
    (_ombox / "omnius-1.json").write_text(json.dumps({"from": "omnius"}), encoding="utf-8")
    check("nor is the orchestrator instructing a desk",
          wd.human_mail_waiting("sysmail.desk") is False)
    (_ombox / "9.json").write_text(json.dumps({"from": "owner", "text": "hi"}), encoding="utf-8")
    check("a message HE sent is", wd.human_mail_waiting("sysmail.desk") is True)
    for f in _ombox.glob("*.json"):
        f.unlink()
    # A guest is a PERSON waiting, so the same guards apply to her mail. Stated
    # as "not a system sender" rather than a list of people, so a guest added
    # after this line is covered without touching the predicate.
    (_ombox / "10.json").write_text(json.dumps({"from": "nina", "text": "idea"}),
                                    encoding="utf-8")
    check("so is a message a GUEST sent", wd.human_mail_waiting("sysmail.desk") is True)
    for f in _ombox.glob("*.json"):
        f.unlink()
    check("start_run opens a tab only when a person actually wrote",
          "human_mail_waiting(session)" in _wsrc
          and "and not has_live_claim and human_mail_waiting" in _wsrc)
    # The loop that produced those heartbeats: a dead claim nobody pruned.
    check("the watchdog prunes provably dead claims itself, without waking anyone",
          "pruned stale claim" in _wsrc)

    # == a live bridge that is not delivering ==================================
    # The last hole in the transport: a bridge can be perfectly alive and still
    # never get mail into a turn (2026-08-02 - "handed to the live bridge", and
    # then nothing, until the owner opened a fourth terminal himself). Liveness
    # cannot detect that; only PROGRESS can.
    print("== bridge delivery deadline ==")
    _dbox = wd.INBOX / "deliver.desk"
    _dbox.mkdir(parents=True, exist_ok=True)
    for f in _dbox.glob("*.json"):
        f.unlink()
    wd.TURNS.mkdir(parents=True, exist_ok=True)
    def _bridge_aged(seconds):
        """Write deliver.desk's presence file as if its bridge started N s ago."""
        stamp = wd.datetime.fromtimestamp(
            _bt.time() - seconds, wd.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        (wd.BRIDGES / "deliver.desk.json").write_text(
            json.dumps({"session": "deliver.desk", "pid": _os0.getpid(),
                        "startedAt": stamp}), encoding="utf-8")

    _bridge_aged(3600)
    _env_d = _dbox / "1.json"
    _env_d.write_text(json.dumps({"id": "1", "channelId": "C-d"}), encoding="utf-8")

    check("fresh mail on a bridged desk is left to the bridge",
          wd.bridge_not_delivering("deliver.desk") is False)
    _old_d = _bt.time() - (wd.BRIDGE_DELIVER_SECONDS + 30)
    _os0.utime(_env_d, (_old_d, _old_d))
    check("mail still unstarted past the deadline means the bridge is NOT delivering",
          wd.bridge_not_delivering("deliver.desk") is True)
    # A running turn is delivery - slow is not the same as broken. The stamp
    # must have a LIVE owner to count (turn_busy, 2026-08-11), so stub one:
    # a genuinely running turn always has a claimed session behind it.
    (wd.TURNS / "deliver.desk.busy").write_text("{}", encoding="utf-8")
    _sa_del = wd.session_alive
    wd.session_alive = lambda s: True
    check("...unless a turn is actually running, however long it takes",
          wd.bridge_not_delivering("deliver.desk") is False)
    check("...but an ORPHANED stamp does not excuse it (the closed-terminal bug)",
          (lambda: (setattr(wd, "session_alive", lambda s: False),
                    wd.bridge_not_delivering("deliver.desk"))[1])() is True)
    wd.session_alive = _sa_del
    # A turn FROZEN on a local dialog is running and going nowhere, and the
    # stamp cannot tell the two apart. 2026-08-04, his first message from home:
    # the ask timed out at 15:29, the session sat on a dialog on a machine he
    # had left, and this one boolean kept every guard quiet for 2.5 hours while
    # two of his messages rotted. The relay had written the proof on disk.
    wd.PERMS.mkdir(parents=True, exist_ok=True)
    (wd.PERMS / "deliver.desk.stalled").write_text("{}", encoding="utf-8")
    check("a turn STALLED on a dialog is not delivery, whatever the stamp says",
          wd.bridge_not_delivering("deliver.desk") is True)
    (wd.PERMS / "deliver.desk.stalled").unlink()
    (wd.TURNS / "deliver.desk.busy").unlink()

    # THE 87-WINDOW STORM, 2026-08-03. Three messages had waited 4.6 hours
    # because his own window held the desk. The moment he said "ok" and the
    # takeover freed it, every replacement bridge was born hours past a
    # 90-second deadline measured on the MAIL, was shot 3s later while still
    # booting, and the next poll opened another one: 87 terminals in four
    # minutes. The deadline must run from whichever came last.
    _bridge_aged(2)
    check("a bridge that just started is NOT overdue on mail that waited hours",
          wd.bridge_not_delivering("deliver.desk") is False)
    check("...and that is the whole storm: a fresh bridge never gets replaced",
          wd.ensure_runner("deliver.desk") == "bridge-owns-desk")
    _bridge_aged(wd.BRIDGE_DELIVER_SECONDS + 30)
    check("once THAT bridge has held the mail past the deadline, it is overdue",
          wd.bridge_not_delivering("deliver.desk") is True)
    # Fail safe in the other direction too: undateable means never overdue.
    (wd.BRIDGES / "deliver.desk.json").write_text(
        json.dumps({"session": "deliver.desk", "pid": _os0.getpid()}), encoding="utf-8")
    check("a bridge with no start time is never shot (unknown != overdue)",
          wd.bridge_not_delivering("deliver.desk") is False)
    _bridge_aged(wd.BRIDGE_DELIVER_SECONDS + 30)
    # Second brake, independent of every clock above.
    wd._run_failures["deliver.desk"] = wd.RUN_FAILURES_BEFORE_ALERT
    check("replacing a desk's window is a remedy, and it stops after N failures",
          wd.bridge_not_delivering("deliver.desk") is False)
    wd._run_failures.clear()
    check("the deadline is measured from the later of mail-arrival and bridge-start",
          "whichever came LAST" in _wsrc)

    _rk4, _rs4 = wd.subprocess.run, wd.start_run
    _killed4 = []
    wd.subprocess.run = lambda *a, **k: _killed4.append(a[0]) or None
    wd.start_run = lambda s, **kw: (spawned.append(s), True)[1]
    _rbm5, _rpc5 = wd.build_map, wd.primary_channel_id
    wd.build_map = lambda schema: {}
    wd.primary_channel_id = lambda m, s: "C-d"
    spawned.clear(); sent.clear(); wd._run_failures.clear(); wd._run_alerted.clear()
    try:
        check("ensure_runner replaces a bridge that is not delivering",
              wd.ensure_runner("deliver.desk") == "bridge-replaced"
              and spawned == ["deliver.desk"])
        check("...by killing the bridge, which is OURS - never his window",
              any("taskkill" in " ".join(str(x) for x in c) for c in _killed4)
              and not (wd.BRIDGES / "deliver.desk.json").exists())
        check("...and the mail is still there to be answered",
              _env_d.is_file())
        # Churning windows all evening is its own failure. Say so instead.
        wd._run_failures["deliver.desk"] = 2
        _bridge_aged(wd.BRIDGE_DELIVER_SECONDS + 30)
        sent.clear()
        wd.ensure_runner("deliver.desk")
        check("repeated replacement is reported, not repeated silently",
              len(sent) == 1 and "keeps not being picked up" in sent[-1][1])
    finally:
        wd.subprocess.run, wd.start_run = _rk4, _rs4
        wd.build_map, wd.primary_channel_id = _rbm5, _rpc5
        for f in _dbox.glob("*.json"):
            f.unlink()
        (wd.BRIDGES / "deliver.desk.json").unlink(missing_ok=True)
        wd._run_failures.clear(); wd._run_alerted.clear(); spawned.clear(); sent.clear()

    # == the deadman ==========================================================
    # 2026-08-03 produced two silent failures in one day and every specific
    # guard missed both. This is the guard for the failures we did NOT predict:
    # queue head not moving + nothing provably alive -> page him. It consults
    # outcomes and raw liveness only, never the classifiers that lied.
    # == !stop — the escape hatch that did not exist ==========================
    # 2026-08-05: he was typing to a desk in Discord, hit RETURN by accident
    # before finishing, and could not take it back. Closing the terminal spawned
    # another (the ENVELOPE was still queued) and !kill reported success while
    # the work carried on. Cancelling work is a different verb from killing a
    # worker.
    print("== !stop (cancel a desk) ==")
    _stbox = wd.INBOX / "stop.desk"
    _stbox.mkdir(parents=True, exist_ok=True)
    for f in _stbox.glob("*.json"):
        f.unlink()
    (_stbox / "1.json").write_text(json.dumps(
        {"id": "1", "from": "owner", "channelId": "C-st", "text": "half typed"}),
        encoding="utf-8")
    wd.BRIDGES.mkdir(parents=True, exist_ok=True)
    (wd.BRIDGES / "stop.desk.json").write_text(json.dumps(
        {"session": "stop.desk", "pid": _os0.getpid(), "cwd": str(SAND),
         "startedAt": "2026-08-05T00:00:00Z"}), encoding="utf-8")
    claim("stop.desk", pid=_os0.getpid())
    (wd.TURNS / "stop.desk.busy").write_text("{}", encoding="utf-8")
    wd._run_failures["stop.desk"] = 2
    wd._native_notified["stop.desk"] = "1.json"

    _rk9 = wd.subprocess.run
    _killed9 = []
    wd.subprocess.run = lambda *a, **k: _killed9.append(a[0]) or type(
        "R", (), {"returncode": 0, "stdout": b"", "stderr": b""})()
    try:
        # The pids must be found BEFORE the state is cleared - the first version
        # wiped the files first and then reported "nothing was running" while
        # the bridge was still alive.
        check("!stop finds the desk's processes before clearing its state",
              _os0.getpid() in wd.desk_processes("stop.desk"))
        _msg9 = wd.stop_session("stop.desk")
        check("...and actually tries to kill them",
              any("taskkill" in " ".join(str(x) for x in c) for c in _killed9))
        check("queued mail is removed, so nothing respawns the desk",
              not list(_stbox.glob("*.json")))
        check("...but MOVED, not deleted - a cancelled instruction is still his",
              (wd.DROPPED / "stop.desk-1.json").is_file())
        check("every piece of desk state is cleared",
              not (wd.BRIDGES / "stop.desk.json").exists()
              and not (wd.SESSIONS / "stop.desk.json").exists()
              and not (wd.TURNS / "stop.desk.busy").exists())
        check("the failure ledger is reset too, so it starts clean next time",
              "stop.desk" not in wd._run_failures
              and "stop.desk" not in wd._native_notified)
        check("afterwards the watchdog wants nothing running there",
              wd.ensure_runner("stop.desk") == "empty")
        check("the reply says what was cancelled and what was killed",
              "stopped" in _msg9.lower() and "set aside" in _msg9)
        check("...and points at where the mail went, since nothing was deleted",
              "state\\dropped" in _msg9)
    finally:
        wd.subprocess.run = _rk9
        (wd.DROPPED / "stop.desk-1.json").unlink(missing_ok=True)
        for f in _stbox.glob("*.json"):
            f.unlink()

    # Survivors are REPORTED, never assumed dead. This is the exact lie that
    # made !kill useless: it said "killed" while the terminal kept working.
    _rdp9 = wd.desk_processes
    wd.desk_processes = lambda s: [999999001, 999999002]
    _rpa9 = wd.pid_alive
    wd.pid_alive = lambda pid, expect=None: pid in (999999001, 999999002)
    wd.subprocess.run = lambda *a, **k: type(
        "R", (), {"returncode": 0, "stdout": b"", "stderr": b""})()
    try:
        _msg10 = wd.stop_session("stop.desk")
        check("a process that SURVIVES the kill is reported, not glossed over",
              "survived" in _msg10.lower() and "999999001" in _msg10)
        check("...and he is told to close it by hand rather than left guessing",
              "by hand" in _msg10.lower())
    finally:
        wd.desk_processes, wd.pid_alive, wd.subprocess.run = _rdp9, _rpa9, _rk9

    check("!stop is a registered control command", "!stop" in wd.CONTROL_COMMANDS)

    # Closing a window must close the desk. Measured 2026-08-05: killing the
    # console host leaves BOTH the bridge and its claude alive and INVISIBLE -
    # `finally` never runs, because a window close is a hard terminate. The
    # desk then looks served by a bridge nobody can see. Verified live after
    # the fix: window closed -> everything gone within 1s, presence withdrawn.
    _bridge_src = (_rr / "tools" / "bridge" / "desk_bridge.py").read_text(encoding="utf-8")
    check("the bridge notices its own window closing",
          "_console_host" in _bridge_src and "console window closed" in _bridge_src)
    check("...and leaves properly, taking claude and its presence file with it",
          "self.withdraw()" in _bridge_src and "terminate(force=True)" in _bridge_src)
    # AST, not grep: the word "psutil" appears in the comment explaining why it
    # is NOT used, which is precisely the docstring-matches-its-own-absence
    # trap lessons.md already records.
    import ast as _brast
    _br_imports = set()
    for _n in _brast.walk(_brast.parse(_bridge_src)):
        if isinstance(_n, _brast.Import):
            _br_imports.update(a.name.split(".")[0] for a in _n.names)
        elif isinstance(_n, _brast.ImportFrom) and _n.module:
            _br_imports.add(_n.module.split(".")[0])
    check("...using a liveness check that needs no extra dependency",
          "OpenProcess" in _bridge_src and "psutil" not in _br_imports,
          f"bridge imports: {sorted(_br_imports)}")
    # Parking cancelled mail inside state\inbox\ would invent a desk named after
    # the folder, because ensure_runners() treats every folder there as one.
    check("dropped mail is kept OUTSIDE state\\inbox\\",
          "inbox" not in str(wd.DROPPED).lower().replace("state\\inbox\\", "X"))

    # The respawn loop itself: a bridge's own claude must never look like his.
    check("a claude started BY a bridge is ours, identified by ancestry",
          "_bridge_owned" in _wsrc and "desk_bridge.py" in _wsrc)
    check("...and native_sessions excludes it, so a desk cannot eat its own tail",
          "not _bridge_owned(p)" in _wsrc)

    print("== a desk that CANNOT start must back off, not retry forever ==")
    # 2026-08-12: state\inbox\wl-integration\ held two envelopes addressed to a
    # desk that is not ours, so cwd_for() resolved a folder that does not exist
    # and start_run returned False BARE - no backoff, no ledger, no alert. It
    # retried every ~3s for 19 hours; 99.2% of watchdog.log was that one line,
    # and status_banner's bot-identity check (last 200 lines) saw only flood.
    wd.RUNNING.clear(); wd._run_failures.clear(); wd._run_backoff.clear()
    wd._run_alerted.clear(); sent.clear()
    _ghbox = wd.INBOX / "ghost.desk"
    _ghbox.mkdir(parents=True, exist_ok=True)
    (_ghbox / "1.json").write_text(json.dumps(
        {"id": "1", "from": "owner", "channelId": "C-gh", "text": "hola"}), encoding="utf-8")
    _rbm8, _rpc8 = wd.build_map, wd.primary_channel_id
    wd.build_map = lambda schema: {}
    wd.primary_channel_id = lambda m, s: "C-gh"
    _rwhich = wd.shutil.which
    try:
        check("a missing desk folder is a FAILED start, not a free retry",
              _real_start_run("ghost.desk") is False
              and wd._run_failures.get("ghost.desk") == 1)
        check("...and it backs off instead of hammering every poll",
              wd._run_backoff.get("ghost.desk", 0) > _bt.time())
        check("...so the very next poll declines to start it again",
              wd.ensure_runner("ghost.desk") == "backoff")
        # It must PAGE, not merely back off: _run_backoff SILENCES the deaf-desk
        # deadman ("a retry is scheduled; its ledger reports itself"), so a bare
        # ledger write would trade a loud bug for a silent one on any desk with
        # a missing folder and real human mail waiting.
        wd._run_backoff.clear(); _real_start_run("ghost.desk")
        wd._run_backoff.clear(); _real_start_run("ghost.desk")
        check("three failures page him once, naming the desk and the reason",
              len(sent) == 1 and "ghost.desk" in sent[-1][1]
              and "folder missing" in sent[-1][1])
        wd._run_backoff.clear(); _real_start_run("ghost.desk")
        check("...and only once, however long it goes on failing",
              len(sent) == 1)
        # The claude-CLI-missing path had the identical bare return. Stub
        # claude_exe, not shutil.which: since 2026-08-14 resolution also
        # consults the registry PATH and the known install locations, so
        # blanking which() on a machine that HAS the CLI proves nothing.
        wd._run_failures.clear(); wd._run_backoff.clear(); wd._run_alerted.clear()
        _rcx, _rbc = wd.claude_exe, wd.broadcast_channel_id
        wd.claude_exe = lambda recheck=False: None
        wd.broadcast_channel_id = lambda name="alerts": "555"   # build_map is stubbed here
        _alerted_before = wd._no_cli_alerted
        wd._no_cli_alerted = False
        check("a missing claude CLI backs off too (same bare-return bug)",
              _real_start_run("demo-app.app") is False
              and wd._run_backoff.get("demo-app.app", 0) > _bt.time())
        check("...and a fleet that can spawn NOTHING says so in Discord, once",
              wd._no_cli_alerted is True and any("fleet is down" in s[1] for s in sent),
              "an hour of silent failure on 2026-08-14 reached only the log file")
        _n_before = len(sent)
        wd._run_backoff.clear(); _real_start_run("demo-app.app")
        check("...and does not repeat it every poll", len(sent) == _n_before)
        wd.claude_exe, wd.broadcast_channel_id = _rcx, _rbc
        wd._no_cli_alerted = _alerted_before
    finally:
        wd.shutil.which = _rwhich
        wd.build_map, wd.primary_channel_id = _rbm8, _rpc8
        for f in _ghbox.glob("*.json"):
            f.unlink()
        _ghbox.rmdir()
        wd._run_failures.clear(); wd._run_backoff.clear(); wd._run_alerted.clear()
        sent.clear()

    print("== deaf-desk deadman ==")
    _ddbox = wd.INBOX / "deadman.desk"
    _ddbox.mkdir(parents=True, exist_ok=True)
    for f in _ddbox.glob("*.json"):
        f.unlink()
    wd._deaf_alerted.clear(); wd._run_failures.clear(); wd._run_alerted.clear()
    wd._run_backoff.clear(); wd._native_notified.pop("deadman.desk", None)
    (wd.SESSIONS / "deadman.desk.json").unlink(missing_ok=True)
    _rbm7, _rpc7 = wd.build_map, wd.primary_channel_id
    wd.build_map = lambda schema: {}
    wd.primary_channel_id = lambda m, s: "C-dd"
    _e1 = _ddbox / "100.json"
    _e1.write_text(json.dumps({"id": "100", "from": "owner", "channelId": "C-dd",
                               "text": "Hola"}), encoding="utf-8")

    def _dd_fires():
        """Reset the once-per-envelope key so each scenario stands alone."""
        wd._deaf_alerted.clear(); sent.clear()
        return wd.deaf_desk_alarm("deadman.desk")

    try:
        check("fresh owner mail never pages (the normal paths get their chance)",
              _dd_fires() is False and sent == [])
        _oldm = _bt.time() - (wd.DEAF_DESK_SECONDS + 60)
        _os0.utime(_e1, (_oldm, _oldm))
        check("old owner mail with NOTHING alive pages him",
              _dd_fires() is True and len(sent) == 1)
        check("...naming the wait, quoting his message, giving the one-word fix",
              "min ago" in sent[-1][1] and "Hola" in sent[-1][1]
              and "!restart" in sent[-1][1])
        check("one page per stuck envelope, not one per poll",
              wd.deaf_desk_alarm("deadman.desk") is False and len(sent) == 1)

        # Every state where the ball is in flight or in HIS court stays quiet.
        class _FakeRun:
            def poll(self):
                return None
        wd.RUNNING["deadman.desk"] = _FakeRun()
        check("an active run suppresses the alarm (someone is working)",
              _dd_fires() is False)
        del wd.RUNNING["deadman.desk"]
        wd._run_backoff["deadman.desk"] = _bt.time() + 300
        check("a scheduled retry suppresses it (the failure ledger reports itself)",
              _dd_fires() is False)
        wd._run_backoff.clear()
        wd._run_alerted.add("deadman.desk")
        check("an already-paged crash loop suppresses it (no double page)",
              _dd_fires() is False)
        wd._run_alerted.clear()
        wd.TAKEOVER.mkdir(parents=True, exist_ok=True)
        (wd.TAKEOVER / "deadman.desk.json").write_text(
            json.dumps({"askedAt": 1}), encoding="utf-8")
        check("a standing takeover question suppresses it (silence is his answer)",
              _dd_fires() is False)
        (wd.TAKEOVER / "deadman.desk.json").unlink()
        wd._native_notified["deadman.desk"] = "100.json"
        check("an ask or decline he already got suppresses it (his court)",
              _dd_fires() is False)
        wd._native_notified.pop("deadman.desk", None)
        _rpa3 = wd.pid_alive
        wd.pid_alive = lambda pid, expect=None: _rpa3(pid)
        claim("deadman.desk", pid=_os0.getpid())
        (wd.TURNS / "deadman.desk.busy").write_text("{}", encoding="utf-8")
        check("a provably LIVE turn suppresses it (long is not deaf)",
              _dd_fires() is False)
        # ...but liveness is not progress, and on a BRIDGE desk the claimed
        # claude pid stays alive between turns forever, so those conditions hold
        # over an idle desk indefinitely. 2026-08-12: a Stop hook resolved the
        # desk id from a cwd that had moved mid-turn, cleared a stamp under
        # another id, and a project desk's .busy outlived its turn - silencing
        # this alarm, the bridge nudge, recover_bridge and the stuck-turn notice
        # at once while his mail sat unread for 35 minutes.
        _riu7 = wd.native_in_use
        try:
            wd.native_in_use = lambda s: 30.0
            check("...a live turn still WRITING stays quiet (no page on honest work)",
                  _dd_fires() is False)
            wd.native_in_use = lambda s: wd.STUCK_QUIET_SECONDS + 60
            check("...but one that has written NOTHING is the 2026-08-12 deadlock",
                  _dd_fires() is True and "nobody working on it" in sent[-1][1])
            wd.native_in_use = lambda s: None
            check("...and cannot-tell keeps the old silence (this only ADDS a page)",
                  _dd_fires() is False)
        finally:
            wd.native_in_use = _riu7
        # The deadman exists for failures nobody predicted, and it still missed
        # this one: alive, mid-turn, and frozen on a dialog. Liveness is not
        # progress - the same lesson as the bridge deadline, one level down.
        (wd.PERMS / "deadman.desk.stalled").write_text("{}", encoding="utf-8")
        check("...but a live turn FROZEN on a dialog is exactly what it must page",
              _dd_fires() is True and "nobody working on it" in sent[-1][1])
        (wd.PERMS / "deadman.desk.stalled").unlink()
        (wd.TURNS / "deadman.desk.busy").unlink()
        wd.pid_alive = _rpa3
        (wd.SESSIONS / "deadman.desk.json").unlink(missing_ok=True)
        _fresh_b = wd.datetime.now(wd.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        (wd.BRIDGES / "deadman.desk.json").write_text(
            json.dumps({"pid": _os0.getpid(), "startedAt": _fresh_b}), encoding="utf-8")
        check("a bridge inside its delivery window suppresses it (not overdue yet)",
              _dd_fires() is False)
        (wd.BRIDGES / "deadman.desk.json").unlink()

        check("...and with every excuse removed it pages again",
              _dd_fires() is True and len(sent) == 1)
        # Progress resets the incident: the queue head moving on means whatever
        # was paged about is over.
        _e1.unlink()
        _e2 = _ddbox / "200.json"
        _e2.write_text(json.dumps({"id": "200", "from": "owner", "channelId": "C-dd",
                                   "text": "otra"}), encoding="utf-8")
        sent.clear()
        check("the queue head moving resets the incident (fresh mail, no page)",
              wd.deaf_desk_alarm("deadman.desk") is False
              and wd._deaf_alerted.get("deadman.desk") is None)
        _os0.utime(_e2, (_oldm, _oldm))
        check("a NEXT envelope going stale is a NEW incident and pages once",
              _dd_fires() is True)
        _e2.unlink()
        _e3 = _ddbox / "300.json"
        _e3.write_text(json.dumps({"id": "300", "from": "heartbeat",
                                   "channelId": "C-dd"}), encoding="utf-8")
        _os0.utime(_e3, (_oldm, _oldm))
        check("system mail rotting never pages him (fleet-internal is a log line)",
              _dd_fires() is False)
        _e3.unlink()

        # Report-only, PROVEN structurally: the alarm's call graph contains no
        # verb that starts, kills or types. AST, not source-grep - the
        # docstring talks about those verbs on purpose.
        import ast as _ast
        _tree = _ast.parse((_rr / "tools" / "discord" / "watchdog.py")
                           .read_text(encoding="utf-8"))
        _dd_fn = next(nd for nd in _ast.walk(_tree)
                      if isinstance(nd, _ast.FunctionDef) and nd.name == "deaf_desk_alarm")
        _calls = set()
        for nd in _ast.walk(_dd_fn):
            if isinstance(nd, _ast.Call):
                f = nd.func
                _calls.add(f.attr if isinstance(f, _ast.Attribute)
                           else getattr(f, "id", ""))
        check("REPORT-ONLY by construction: it never starts, kills or types",
              not _calls & {"start_run", "open_tab", "recover_bridge", "run",
                            "Popen", "kill_session", "close_native_sessions"})
        check("the deadman runs on every poll pass, after the normal machinery",
              "deaf_desk_alarm(box.name)" in _wsrc)
    finally:
        wd.build_map, wd.primary_channel_id = _rbm7, _rpc7
        wd.RUNNING.pop("deadman.desk", None)
        wd._run_backoff.clear(); wd._run_alerted.clear(); wd._deaf_alerted.clear()
        for f in _ddbox.glob("*.json"):
            f.unlink()
        (wd.TURNS / "deadman.desk.busy").unlink(missing_ok=True)
        (wd.SESSIONS / "deadman.desk.json").unlink(missing_ok=True)
        (wd.BRIDGES / "deadman.desk.json").unlink(missing_ok=True)
        sent.clear()

    # == desktop-app conversations are not desks ==============================
    # The end of the 2026-08-03 chain: the takeover ask aimed at a background
    # conversation hosted by the Claude desktop APP, and "ok" would have
    # killed the chat he was typing into. App-hosted claude processes are
    # chats, never desk terminals.
    print("== desktop-app squatters ==")
    import sys as _sys0
    _want_cwd = str(wd.cwd_for("squat.desk"))

    class _FProc:
        def __init__(self, pid, cwd, anc_exe=None, broken=False):
            self.info = {"pid": pid, "name": "claude.exe"}
            self._cwd, self._anc, self._broken = cwd, anc_exe, broken
        def cwd(self):
            return self._cwd
        def parents(self):
            if self._broken:
                raise RuntimeError("access denied")
            if not self._anc:
                return []
            class _A:
                def __init__(self, exe):
                    self._e = exe
                def exe(self):
                    return self._e
            return [_A(self._anc)]

    class _FakePsutil:
        _procs = []
        @classmethod
        def process_iter(cls, attrs=None):
            return list(cls._procs)

    _FakePsutil._procs = [
        _FProc(101, _want_cwd),                                     # his terminal
        _FProc(102, _want_cwd,
               anc_exe=r"C:\Users\x\AppData\Local\AnthropicClaude\app-1.2\claude.exe"),
        _FProc(103, _want_cwd, broken=True),                        # unreadable ancestry
        _FProc(104, r"C:\somewhere\else"),                          # other folder
    ]
    _real_psutil = _sys0.modules.get("psutil")
    _sys0.modules["psutil"] = _FakePsutil
    try:
        _got = wd.native_sessions("squat.desk")
        check("a terminal claude in the desk folder is HIS session",
              101 in _got)
        check("a desktop-APP conversation in that folder is NOT (it is a chat)",
              102 not in _got)
        check("unreadable ancestry fails open to asking (the safe verb)",
              103 in _got)
        check("other folders were never candidates",
              104 not in _got)
    finally:
        if _real_psutil is not None:
            _sys0.modules["psutil"] = _real_psutil
        else:
            _sys0.modules.pop("psutil", None)

    print("== headless run command line (source) ==")
    src = (HERE / "watchdog.py").read_text(encoding="utf-8")
    check("kill_session clears the run lease (else !restart kills and never returns)",
          'RUNS / f"{session}.json").unlink' in src)
    check("!restart reports a failed start instead of claiming success",
          "RUN COULD NOT START" in src)
    check("a busy desk logs the queue reason instead of logging 'spawned'",
          "queued behind the active run" in src)
    # window:"terminal" (owner, 2026-08-01): the FIRST session on a work desk
    # is a visible tab; ONLY open_tab may make windows, and only that once -
    # follow-up mail rides headless --continue runs in the same conversation.
    check("visible tabs exist again, but only via open_tab",
          "def open_tab" in src and '"new-tab"' in src)
    # A bare claude tab is warm but UNREACHABLE: mail arriving later would
    # start a headless --continue run against the conversation sitting idle in
    # that window. Two writers, one transcript. The window must BE the bridge.
    # AST, not grep: the docstring explains why it is not a bare claude tab,
    # and matching prose has now cost five tests in this file.
    import ast as _ast3
    _otfn = next(n for n in _ast3.walk(_ast3.parse(src))
                 if isinstance(n, _ast3.FunctionDef) and n.name == "open_tab")
    if _ast3.get_docstring(_otfn):
        _otfn.body = _otfn.body[1:]          # drop the prose, keep the code
    _otcode = _ast3.unparse(_otfn)           # unparse also drops every comment
    check("a desk window runs the BRIDGE, never a bare claude tab",
          "desk_bridge.py" in _otcode and "claude" not in _otcode)
    check("the tab is named after the desk, not 'Claude Code'",
          "'--title', session" in _otcode)
    # cmd /k outlives the program, so every kill/restart left a dead shell tab
    # behind and they piled up (owner, 2026-08-02: "daybook opened 2 tabs").
    check("a desk window closes with its desk (cmd /c, never /k)",
          "'/c'" in _otcode and "'/k'" not in _otcode)
    check("...but a failed start still leaves something readable on screen",
          "|| pause" in _otcode)
    _wk = (_rr / "wakeup-omnius.bat").read_text(encoding="utf-8")
    check("the hand launcher does the same", "cmd /c python" in _wk and "cmd /k" not in _wk)
    check("the tab decision needs BOTH terminal mode and no live claim",
          "not has_live_claim" in src)

    print("== project #general routing ==")
    _sch2 = {"initial": {"categories": [{"name": "🎛 ORCHESTRATOR"}]},
             "prefixes": {"project": "📁 ", "archived": "🗄 "}}
    _rgc = api.guild_channels
    # sandbox projects: alpha has one component (app), demo-app has two
    api.guild_channels = lambda: [
        {"id": "CAT_S", "name": "📁 alpha", "type": api.CHANNEL_CATEGORY},
        {"id": "CAT_M", "name": "📁 demo-app", "type": api.CHANNEL_CATEGORY},
        {"id": "GEN_S", "name": "general", "type": api.CHANNEL_TEXT, "parent_id": "CAT_S"},
        {"id": "GEN_M", "name": "general", "type": api.CHANNEL_TEXT, "parent_id": "CAT_M"},
    ]
    try:
        _m2 = wd.build_map(_sch2)
        check("single-component project: #general routes straight to the desk "
              "('not you again', owner 2026-08-01)",
              _m2["GEN_S"].session == "alpha.app")
        check("multi-component project: #general still goes to the orchestrator "
              "(which desk IS a real question)",
              _m2["GEN_M"].session == "orchestrator")
    finally:
        api.guild_channels = _rgc

    # == notices are for problems and questions only ==========================
    # Owner, 2026-08-03: "i do not need any info messages at all, just issues or
    # error message, or important stuff". Everything that was merely TRUE has
    # been cut; what remains either needs an answer or reports a fault.
    print("== no info messages ==")
    sent.clear()
    wd.hello_post({"CID_FS": T("fleet-status", None), "CID_O": T("orchestrator", "orchestrator")})
    check("startup is logged, never announced (15 of these in one day)",
          sent == [])
    _wsrc_n = (HERE.parent.parent / "tools" / "discord" / "watchdog.py").read_text(encoding="utf-8")
    check("no 'still working' progress notice survives",
          "is on it" not in _wsrc_n and "it will answer when it wakes" not in _wsrc_n)
    check("a desk nobody is running IS still reported (that is a real problem)",
          "nothing is running on that desk" in _wsrc_n)
    _teh_n = (HERE.parent.parent / "tools" / "discord" / "turn_end_hook.py").read_text(encoding="utf-8")
    check("a turn that changed nothing says nothing at all",
          "changed no files" not in _teh_n)
    check("...but work that went unreported still speaks",
          "changed files but reported nothing" in _teh_n)

    print("== skill wiring ==")
    real_root = HERE.parent.parent
    root_skill = real_root / ".claude" / "skills" / "omnius" / "SKILL.md"
    stub_skill = real_root / "templates" / "project" / ".claude" / "skills" / "omnius" / "SKILL.md"
    check("root /omnius skill exists", root_skill.is_file())
    # 2026-08-04: a project desk was spawned with a mandate, got NO mail, and
    # posted to Discord twice while he sat at that terminal reading the same
    # text. "Why do I get messages in discord if i am in cli?" Because /omnius
    # said "reply via outbox" and nothing scoped that to mail, or to the run.
    # Whitespace-normalised: asserting on prose must not depend on where the
    # markdown happens to wrap, or the next reflow "breaks" a rule that is
    # still there.
    _sk_reply = " ".join(root_skill.read_text(encoding="utf-8").split())
    check("the contract posts only for mail it actually received",
          "Only answer Discord if Discord asked" in _sk_reply
          and "no envelope, no post" in _sk_reply)
    # ...and the same rule in the ONE file every session loads whatever way it
    # started. A desk he opens by hand never invokes /omnius, so a rule that
    # lives only in the skill cannot reach it: that desk was restarted on
    # 2026-08-04 and went straight back to posting.
    _root_md = " ".join((real_root / "CLAUDE.md").read_text(encoding="utf-8").split())
    check("the constitution carries it too (hand-opened desks never read the skill)",
          "Answer where you were asked" in _root_md and "No envelope" in _root_md)
    check("...and it stops governing once the run ends (keyboard turns are not mail)",
          "This contract ends when the run does" in _sk_reply
          and "answer him at the keyboard and post nothing" in _sk_reply)
    check("project template ships the /omnius stub (spawned sessions only see project skills)",
          stub_skill.is_file() and "workspace-root" in stub_skill.read_text(encoding="utf-8"))
    # Skills do not cross folders: --add-dir grants file access, not skill
    # discovery. Every ROOT skill a project desk should reach needs a stub, and
    # /watch was missing one - "Unknown skill: watch" from a component desk on
    # 2026-08-03, while the skill sat installed at the root.
    _watch_stub = real_root / "templates" / "project" / ".claude" / "skills" / "watch" / "SKILL.md"
    check("project template ships the /watch stub too",
          _watch_stub.is_file() and "workspace-root" in _watch_stub.read_text(encoding="utf-8"))
    check("...and points SKILL_DIR at the ROOT (the scripts live there, not in the project)",
          "SKILL_DIR" in _watch_stub.read_text(encoding="utf-8"))
    # Stubs belong at PROJECT level only. A copy in each component folder is
    # discovered as well, so the picker showed "/watch" twice (2026-08-03).
    _dupes = [str(p.relative_to(real_root))
              for p in real_root.glob("projects/*/*/.claude/skills/*/SKILL.md")]
    check("no component-level skill stubs (they duplicate the project's)",
          _dupes == [], f"duplicated at {_dupes}")
    wd_src = (real_root / "tools" / "discord" / "watchdog.py").read_text(encoding="utf-8")
    check("watchdog spawns with /omnius", '"/omnius"' in wd_src)
    check("watchdog spawn passes --add-dir (sandbox must reach the workspace root)", "--add-dir" in wd_src)
    tset = json.loads((real_root / "templates" / "project" / ".claude" / "settings.json").read_text(encoding="utf-8"))
    tperm = tset["permissions"]
    check("template profile: workspace root as additional directory", "../.." in tperm.get("additionalDirectories", []))
    check("template profile: root .env denied via cross-root path",
          "Read(../../.env)" in tperm["deny"] and "Read(../../**/.env)" in tperm["deny"])

    # == pack -Work ============================================================
    # A work instance goes onto EMPLOYER hardware. A dropped exclusion here does
    # not fail loudly - it silently ships personal notes to another company's PC.
    # == the repo IS the product ==============================================
    # His words: "el repo es para hacer instalacion fresh". pack.ps1 -Fresh has
    # always filtered this instance's paperwork out of a RELEASE; git was the
    # only place it still travelled, so a clone carried notes about this
    # machine into every new one. Anything -Fresh refuses to ship must not be
    # tracked either - one rule, two enforcers.
    print("== the repo is the product ==")
    if (real_root / ".git").exists():
        _tracked = subprocess.run(["git", "ls-files"], cwd=str(real_root),
                                  capture_output=True, text=True).stdout.split()
        check("git ls-files works, so this check means something", bool(_tracked))
        _paperwork = [f for f in _tracked
                      if f == "START-HERE.md" or "HANDOVER" in f
                      or f.startswith("docs/HANDOFF-")]
        check("no instance paperwork is tracked (-Fresh already refuses it)",
              not _paperwork, f"tracked but not product: {_paperwork}")
        for _never in ("memory/", "media/", "projects/", "daybook/notes/",
                       "config/omnius.ini", "config/email.ini",
                       "config/allow-learned.json", "config/fleet.json"):
            _hit = [f for f in _tracked if f.startswith(_never)
                    and f != "projects/.gitkeep"]
            check(f"nothing under {_never} is tracked",
                  not _hit, f"leaks this instance: {_hit[:3]}")

        # And the CONTENT, not just the file list: every tracked text file is
        # scanned against config\audit-sentinels.txt. The names used to live in
        # comments as incident records ("<project>.web did exactly that...") -
        # honest history, and exactly what a public tree must not carry. On a
        # fresh instance there is no sentinel file and this collapses to a
        # no-op, which is correct: it has no names to leak yet.
        # Nothing is exempt any more. settings.json used to be, for its hook
        # COMMANDS - machine-absolute paths by design - and that exemption is
        # precisely how one machine's home directory reached GitHub in six
        # files (2026-08-14). Hooks live in settings.local.json now, so the
        # tracked tree has no legitimate reason to hold anybody's path.
        _sents = []
        _sf2 = real_root / "config" / "audit-sentinels.txt"
        if _sf2.is_file():
            for _ln in _sf2.read_text(encoding="utf-8-sig").splitlines():
                _ln = _ln.strip()
                if _ln and not _ln.startswith("#"):
                    _sents.append(re.compile(_ln.split("=>")[0].strip(), re.I))
        _dirty = []
        for _f in _tracked:
            if _f.endswith((".png", ".ico", ".jpg", ".zip")):
                continue
            try:
                _body = (real_root / _f).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for _rx in _sents:
                if _rx.search(_body):
                    _dirty.append(f"{_f} ({_rx.pattern[:30]})")
                    break
        check("no tracked text file carries a sentinel name (public-tree clean)",
              not _dirty, f"{_dirty[:5]}")

        # The sentinel file is gitignored, so it protects THIS instance and
        # nobody else. This one needs no list: a real home directory has a
        # shape, and documentation writes it with a placeholder instead.
        _homes = re.compile(r"[A-Za-z]:[\\/]{1,2}Users[\\/]{1,2}([^\\/\"'\s<>]+)", re.I)
        _placeholders = {"you", "<you>", "user", "username", "yourname", "<user>",
                         "%username%", "$env:username"}
        _paths = []
        for _f in _tracked:
            # This file has to contain the pattern it hunts for, and pack.ps1
            # documents the same shape - the exemption a guard is allowed.
            if _f.endswith((".png", ".ico", ".jpg", ".zip")) \
                    or _f in ("tools/discord/test_watchdog.py", "tools/release_sanitize.py"):
                continue
            try:
                _body = (real_root / _f).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for _m in _homes.finditer(_body):
                if _m.group(1).lower().strip("<>%$") not in \
                        {p.strip("<>%$") for p in _placeholders}:
                    _paths.append(f"{_f}: {_m.group(0)[:40]}")
                    break
        check("no tracked file names a real home directory (use a placeholder)",
              not _paths, f"{_paths[:5]}")
    else:
        check("no .git here - a fresh instance, nothing to check", True)

    print("== pack -Work ==")
    pack_src = (real_root / "pack.ps1").read_text(encoding="utf-8")
    check("pack: -Work switch exists", "[switch]$Work" in pack_src)
    check("pack: work archive is named distinctly (never overwrites the full one)",
          "'work-'" in pack_src)
    for leftover in ("daybook/notes", "media"):
        check(f"pack -Work excludes {leftover}", f'--exclude=$leaf/{leftover}"' in pack_src)
    check("pack -Work excludes projects BY NAME (so tracked projects/.gitkeep still travels)",
          'projects/$($p.Name)' in pack_src and "-Directory" in pack_src)

    # The -Work guard. The switch reads as "the one for the work PC", which is
    # backwards on a work machine: there, daybook notes and projects ARE the
    # work. The guard turns that silent drop into a visible, refusable one.
    check("pack -Work: measures what it drops instead of assuming",
          "Get-DropInventory" in pack_src)
    check("pack -Work: has a -Yes escape hatch for non-interactive callers",
          "[switch]$Yes" in pack_src)
    check("pack -Work: refuses rather than prompting into a pipe (never reads stdin as consent)",
          "IsInputRedirected" in pack_src)
    # Order matters more than presence: a guard that runs after tar has already
    # written the archive is decoration, not a guard.
    check("pack -Work: guard runs BEFORE tar, so aborting writes no archive",
          pack_src.index("Get-DropInventory -Root") < pack_src.index("& $tar @tarArgs"))
    # The old line named all three paths unconditionally, so it read identically
    # on a pack that lost nothing and one that lost every note you had.
    check("pack -Work: post-pack summary reports the real drop, not a static list",
          "LEFT BEHIND: daybook\\notes\\, projects\\<name>\\, media\\" not in pack_src)

    # Key material. .gitignore has no say over the archive, and the archive is the
    # WIDER exposure: it becomes a GitHub release asset and a cloud-folder copy. A
    # key git correctly refuses to commit still ships in the zip.
    for glob in ("*serviceAccount*.json", "*service-account*.json", "*adminsdk*.json",
                 "*-sa.json", "*.p12", "*.jks", "*.keystore"):
        check(f"pack excludes key shape {glob}", f"'{glob}'" in pack_src)
    check("pack: names excluded key material instead of dropping it silently",
          "secretHits" in pack_src and "EXCLUDED from the archive" in pack_src)
    # Guard against someone widening this later: a bare *.key would silently drop
    # legitimate files, which is the exact class of bug --exclude=state already was.
    check("pack: does NOT exclude a bare *.key (too broad -> silent loss)",
          "'*.key'" not in pack_src)

    # The tar exclusion only works because none of this is in git: anything ever
    # committed still travels inside the bundled .git\ no matter what tar skips.
    gitignore = (real_root / ".gitignore").read_text(encoding="utf-8")
    for pat in ("media/", "projects/*"):
        check(f"pack -Work: {pat} is gitignored (else .git\\ leaks it past the exclusion)",
              pat in gitignore)
    # daybook/notes/ was in that list until 2026-07-31, when the user chose to track
    # the notes in the private repo. That silently voided -Work's promise: tar skips
    # the working copy but the bundled .git\ still carries every committed note. The
    # honest fix is not to re-ignore them - it is for -Work to refuse.
    check("pack -Work: refuses when leave-behind content is TRACKED (tar cannot beat .git\\)",
          "ls-files" in pack_src and "TRACKED IN GIT" in pack_src)
    check("pack -Work: that refusal writes no archive",
          "No archive written" in pack_src)

    # == spawn: model + effort =================================================
    # Every desk runs Opus 5 at xhigh (user decision 2026-07-31). Asserted against
    # the real command line, not the constants - the constants being right proves
    # nothing if the flags never reach `claude`.
    print("== run command: model + effort + headless ==")
    import subprocess as _sp, shutil as _sh
    _real_popen, _real_which = _sp.Popen, _sh.which
    captured = []
    _sp.Popen = lambda args, **kw: (captured.append((args, kw)), FakeProc())[1]
    _sh.which = lambda name: f"C:\\fake\\{name}.exe"
    # Force the headless branch: the real fleet.json makes project desks
    # window:"terminal" now, and this section tests the RUN command line.
    _cfg_hold = wd.FLEET_CFG
    wd.FLEET_CFG = SAND / "no-such-fleet.json"

    def _last_cmd():
        return " ".join(captured[-1][0]) if captured else ""
    try:
        check("default model is opus", wd.DEFAULT_MODEL == "opus")
        check("default effort is xhigh", wd.DEFAULT_EFFORT == "xhigh")

        captured.clear(); _real_start_run("demo-app.app")
        check("run passes --model opus by default", "--model opus" in _last_cmd())
        check("run passes --effort xhigh by default", "--effort xhigh" in _last_cmd())
        check("the run is HEADLESS: -p with /omnius as the prompt, last",
              captured and list(captured[-1][0][-2:]) == ["-p", "/omnius"])
        check("the claude child itself opens no window (creationflags set)",
              captured and captured[-1][1].get("creationflags") == wd.NO_WINDOW)
        check("the run's environment carries its identity token",
              captured and (captured[-1][1].get("env") or {}).get("OMNIUS_RUN_ID"))
        check("...and the lease carries the SAME token (identity by proof, not inference)",
              captured and json.loads((wd.RUNS / "demo-app.app.json").read_text(encoding="utf-8"))
              .get("runId") == captured[-1][1]["env"]["OMNIUS_RUN_ID"])
        check("stdout/stderr go to the run log, not nowhere",
              captured and captured[-1][1].get("stdout") is not None
              and (wd.LOGS / "runs" / "demo-app.app.log").is_file())
        check("a run writes its lease with the child pid",
              (wd.RUNS / "demo-app.app.json").is_file()
              and json.loads((wd.RUNS / "demo-app.app.json").read_text(encoding="utf-8"))["pid"] == 1)

        captured.clear(); _real_start_run("demo-app.app", model="sonnet", effort="low")
        check("a per-desk override wins over the default",
              "--model sonnet" in _last_cmd() and "--effort low" in _last_cmd())

        # A bad value must not reach the CLI: an unknown flag value would make
        # every run die with no obvious cause - the bad-.env lesson.
        captured.clear(); _real_start_run("demo-app.app", effort="turbo")
        check("invalid effort falls back to xhigh and is never passed through",
              "--effort xhigh" in _last_cmd() and "turbo" not in _last_cmd())

        # The tab branch is OPT-IN since 2026-08-03 (the owner starts desks
        # himself), so this needs a config that actually asks for a window.
        (SAND / "tools" / "bridge").mkdir(parents=True, exist_ok=True)
        (SAND / "tools" / "bridge" / "desk_bridge.py").write_text("", encoding="utf-8")
        _termcfg = SAND / "fleet-terminal.json"
        _termcfg.write_text(json.dumps({"defaults": {"model": "opus", "effort": "xhigh",
                                                     "window": "terminal"}}), encoding="utf-8")
        wd.FLEET_CFG = _termcfg
        (wd.SESSIONS / "demo-app.app.json").unlink(missing_ok=True)
        wd.RUNNING.clear()                      # the headless tests above left a FakeProc
        for f in wd.RUNS.glob("*.json"):
            f.unlink()
        captured.clear(); _real_start_run("demo-app.app")
        check("terminal-mode desk with no claim: first contact opens a TAB",
              captured and "new-tab" in captured[-1][0])
        _lease_t = json.loads((wd.RUNS / "demo-app.app.json").read_text(encoding="utf-8"))
        check("the tab lease is mode:terminal (no pid - wt detaches)",
              _lease_t.get("mode") == "terminal" and "pid" not in _lease_t)
        check("the tab lease holds the desk while claude boots (no tab-per-pass)",
              wd.run_active("demo-app.app"))
        claim("demo-app.app", pid=_pid_os.getpid())
        check("once the session claims, the claim governs and the lease is gone",
              not wd.run_active("demo-app.app")
              and not (wd.RUNS / "demo-app.app.json").exists())
        # ...and with that live claim, later mail rides a HEADLESS follow-up.
        captured.clear(); _real_start_run("demo-app.app")
        check("terminal-mode desk with a live claim: follow-up is headless -p",
              captured and list(captured[-1][0][-2:]) == ["-p", "/omnius"])
        (wd.SESSIONS / "demo-app.app.json").unlink(missing_ok=True)
    finally:
        _sp.Popen, _sh.which = _real_popen, _real_which
        wd.FLEET_CFG = _cfg_hold
        wd.RUNNING.clear()
        for f in wd.RUNS.glob("*.json"):
            f.unlink()

    # == the !reload loop (2026-07-31) =========================================
    # The cursor advanced in memory, handle_message re-execed the process, and the
    # end-of-pass write never ran - so the next process re-read the SAME !reload
    # and re-execed, ~3s per cycle, forever. Nothing recovers from that by waiting.
    # The invariant: the cursor must be DURABLE BEFORE the message is acted on.
    # == fleet.json: per-role permission modes ================================
    # User decision 2026-07-31 option (b): component desks bypass so an unattended
    # desk never stalls on a prompt nobody is there to click; the ORCHESTRATOR
    # keeps its profile because it can !killall, push and delete projects.
    # == alive vs listening =====================================================
    # A session frozen on a local permission dialog used to be indistinguishable
    # from a healthy one - pid alive, watcher alive, lastSeenAt 3s old, !status
    # "on". The heartbeat is written by a SEPARATE process, so it kept stamping.
    print("== stall detection (alive vs listening) ==")
    wd.PERMS.mkdir(parents=True, exist_ok=True)
    for f in wd.PERMS.glob("*"):
        f.unlink()
    check("a quiet desk gets no stall note", wd.stall_note("demo-app.app") == "")

    (wd.PERMS / "abc123def456.json").write_text(json.dumps(
        {"id": "abc123def456", "code": "def456", "session": "demo-app.app", "tool": "Bash"}),
        encoding="utf-8")
    open_note = wd.stall_note("demo-app.app")
    check("an open request is reported with its code", "def456" in open_note and "#alerts" in open_note)
    check("an open request on ONE desk does not smear onto another",
          wd.stall_note("demo-app.backend") == "")

    (wd.PERMS / "demo-app.app.stalled").write_text(json.dumps(
        {"session": "demo-app.app", "since": "2026-07-31T16:09:00Z", "tool": "Bash", "code": "865953"}),
        encoding="utf-8")
    stalled = wd.stall_note("demo-app.app")
    check("a timed-out desk is reported as STALLED, with when", "STALLED" in stalled and "16:09" in stalled)
    check("STALLED outranks a merely-open request", "#alerts" not in stalled)

    # The clears must be mechanical - the whole point is that nothing remembers.
    _real_kill_session("demo-app.app")
    check("kill_session clears the stall marker", not (wd.PERMS / "demo-app.app.stalled").is_file())
    (wd.PERMS / "demo-app.app.stalled").write_text("{}", encoding="utf-8")
    _rp2, _rw2 = _sp.Popen, _sh.which
    _sp.Popen = lambda args, **kw: FakeProc()
    _sh.which = lambda n: f"C:\\fake\\{n}.exe"
    try:
        _real_start_run("demo-app.app")
    finally:
        _sp.Popen, _sh.which = _rp2, _rw2
        wd.RUNNING.clear()
        for f in wd.RUNS.glob("*.json"):
            f.unlink()
    check("start_run clears the stall marker", not (wd.PERMS / "demo-app.app.stalled").is_file())
    check("a corrupt marker still reports STALLED rather than crashing !status",
          ((wd.PERMS / "demo-app.app.stalled").write_text("{ not json", encoding="utf-8"),
           "STALLED" in wd.stall_note("demo-app.app"))[1])
    for f in wd.PERMS.glob("*"):
        f.unlink()

    # The relay must WRITE that marker on timeout, or none of the above ever fires.
    relay_src = (real_root / "tools" / "discord" / "permission_relay.py").read_text(encoding="utf-8")
    check("relay writes a durable stall marker when it times out",
          '.stalled"' in relay_src and "the action stays blocked" in relay_src)
    check("relay clears a stale marker when the same desk asks again",
          'unlink(missing_ok=True)' in relay_src and '.stalled' in relay_src)

    # == backlog notices ========================================================
    # 2026-08-01: the owner waited 13 minutes and sent "Holaaa???". The envelope
    # had been delivered instantly - the desk simply was not awake, so nothing
    # could acknowledge. The ack rule only covers awake-but-slow. This notice
    # comes from the watchdog, which is the only always-on piece, and is
    # cause-agnostic: undrained means not-being-handled, whatever the reason.
    # == one watcher per desk ===================================================
    # 2026-08-01: two watchers were armed on one desk and BOTH delivered the same
    # envelope, so the session nearly answered twice. Same family as the
    # duplicate-spawn incident. Source-level checks on purpose: exercising the
    # guard for real needs inbox_watch's own ROOT, and it would write a claim
    # into the LIVE state\sessions\ - the mistake lessons.md already records.
    # == memory budget =========================================================
    # Adopted from Hermes' memory_tool (2026-08-01): it bounds each memory file by
    # CHARACTERS, "not tokens, because char counts are model-independent". We had
    # no bound at all, which is how status.md reached 65 KB - big enough that
    # reading it hit the context cap. Yesterday's split was manual, so it would
    # simply drift back; a number that fails the suite will not.
    #
    # These are budgets, not limits of taste: raise them deliberately if the
    # content genuinely earns it, but raise them in a commit that says why.
    # == schedule: grace + success ==============================================
    # Adopted from Hermes' cron after reading its source (2026-08-01). It matters
    # here specifically because this machine is a laptop that sleeps.
    # == --continue only where there is history ================================
    # 2026-08-01, caught live: `claude --continue` in a folder with NO history
    # does not fail - it attaches to the most recent conversation from elsewhere.
    # Spawning a brand-new desk resumed the ORCHESTRATOR's conversation inside
    # that desk's folder: it ran with the wrong context, never executed /omnius
    # for its own desk, never claimed, never read its brief. The `||` fallback is
    # useless because the first command succeeds at doing the wrong thing.
    print("== --continue guard ==")
    check("history_dir_for encodes the cwd the way Claude Code does",
          wd.history_dir_for("D:\\workspaces\\omnius").name == "D--workspaces-omnius")

    # THREE ways a desk is launched, and all three must resume its own
    # conversation - otherwise "work at the desk, continue from Discord later"
    # silently becomes a fresh session. The bridge was the odd one out until
    # 2026-08-13: a live bridge is warm because it never restarts, so nobody
    # noticed that when it DOES (!restart, crash, reboot) it started empty.
    for _who, _src in (
            ("watchdog start_run", (real_root / "tools" / "discord" / "watchdog.py")),
            ("fleet_ops.open_desk", (real_root / "tools" / "orchestrator" / "fleet_ops.py")),
            ("desk_bridge", (real_root / "tools" / "bridge" / "desk_bridge.py"))):
        if not _src.is_file():
            check(f"{_who}: not present on this instance, nothing to check", True)
            continue
        _s = _src.read_text(encoding="utf-8")
        check(f"{_who} resumes the desk's own conversation", "--continue" in _s,
              "a launch path without it turns 'restart' into 'forget'")
        check(f"...and {_who} gates it on has_history", "has_history" in _s,
              "in a virgin folder --continue attaches to ANOTHER folder's chat")
        # ...and on the desk's RESUME POLICY, which is the same invariant seen
        # from the other side: resuming is right by default, and wrong for a
        # desk fleet.json marks `fresh`. The paths learned it one at a time -
        # start_run 2026-08-01, the bridge 2026-08-16 (a takeover dragged 5.9 MB
        # into every Discord line), open_desk the same day. Checking it HERE, in
        # the loop over every launch path, is what makes the answer to "does
        # this hold for all desks, including future ones?" a yes with a test
        # behind it: a fourth door added later fails this check on arrival.
        check(f"...and {_who} honours the desk's resume policy",
              'resume_mode != "fresh"' in _s,
              "a path deaf to resume:fresh re-imports the ~200x context tax")

    _real_home = Path.home
    fake_home = SAND / "home"
    (fake_home / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
    Path.home = staticmethod(lambda: fake_home)
    try:
        (wd.SESSIONS / "demo-app.app.json").unlink(missing_ok=True)   # no human here
        wd.RUNNING.clear()
        fresh = SAND / "projects" / "demo-app" / "app"
        check("a folder with no history reports none", not wd.has_history(fresh))
        d = wd.history_dir_for(fresh)
        d.mkdir(parents=True, exist_ok=True)
        check("an EMPTY history folder still counts as none", not wd.has_history(fresh))
        (d / "session.jsonl").write_text("{}", encoding="utf-8")
        check("a folder with a real transcript reports history", wd.has_history(fresh))

        # No claim here on purpose: a live claim means a HUMAN owns this
        # conversation, and start_run then refuses --continue by design. That
        # case gets its own check below. Headless config too - with the real
        # fleet.json an unclaimed desk opens a WINDOW instead of running.
        (wd.SESSIONS / "demo-app.app.json").unlink(missing_ok=True)
        _headless_cfg = SAND / "fleet-headless.json"
        _headless_cfg.write_text(json.dumps({"defaults": {"model": "opus", "effort": "xhigh",
                                                          "window": "headless"}}), encoding="utf-8")
        _cfg_before = wd.FLEET_CFG
        wd.FLEET_CFG = _headless_cfg
        _rp3, _rw3 = _sp.Popen, _sh.which
        cap3 = []
        _sp.Popen = lambda args, **kw: (cap3.append(args), FakeProc())[1]
        _sh.which = lambda n: f"C:\\fake\\{n}.exe"
        try:
            cap3.clear(); _real_start_run("demo-app.app")
            with_hist = " ".join(cap3[-1])
            check("with history, the run uses --continue", "--continue" in with_hist)
            (d / "session.jsonl").unlink()
            cap3.clear(); _real_start_run("demo-app.app")
            no_hist = " ".join(cap3[-1])
            check("with NO history, the run does NOT use --continue", "--continue" not in no_hist)
            check("and it still runs /omnius so the desk handles its mail", "/omnius" in no_hist)

            # A human's terminal outranks the resume policy: --continue would
            # make the run a second writer in the conversation he is sitting
            # in (owner works desks manually since 2026-08-03).
            (d / "session.jsonl").write_text("{}", encoding="utf-8")
            claim("demo-app.app", pid=_os0.getpid())
            cap3.clear(); _real_start_run("demo-app.app")
            held = " ".join(cap3[-1])
            check("a desk held by a live terminal runs FRESH, never --continue",
                  "--continue" not in held and "/omnius" in held)
            (wd.SESSIONS / "demo-app.app.json").unlink(missing_ok=True)
            (d / "session.jsonl").unlink(missing_ok=True)

            # resume:"fresh" beats even a real history: the orchestrator pays
            # memory + bus transcript, never the 11 MB dev saga.
            # resume:"fresh" from the shipped roles, but headless so we see the
            # RUN command line rather than a window being opened.
            _freshcfg = SAND / "fleet-fresh.json"
            # A fresh clone has only the example: install.ps1 copies it to
            # fleet.json, and config\* is gitignored. Reading only the real one
            # made the whole suite die with a traceback on a clone - which is
            # the one moment someone is deciding whether to trust this repo
            # (2026-08-14). The example IS the shipped defaults, so it answers
            # the same question.
            _fleet_src = real_root / "config" / "fleet.json"
            if not _fleet_src.is_file():
                _fleet_src = real_root / "config" / "fleet.example.json"
            _shipped = json.loads(_fleet_src.read_text(encoding="utf-8"))
            _shipped["defaults"]["window"] = "headless"
            _freshcfg.write_text(json.dumps(_shipped), encoding="utf-8")
            wd.FLEET_CFG = _freshcfg
            d_orch = wd.history_dir_for(SAND)
            d_orch.mkdir(parents=True, exist_ok=True)
            (d_orch / "session.jsonl").write_text("{}", encoding="utf-8")
            cap3.clear(); _real_start_run("orchestrator")
            orch_cmd3 = " ".join(cap3[-1]) if cap3 else ""
            check("a fresh-configured desk NEVER uses --continue, history or not",
                  "--continue" not in orch_cmd3 and "/omnius" in orch_cmd3)
            (d_orch / "session.jsonl").unlink()
        finally:
            _sp.Popen, _sh.which = _rp3, _rw3
            wd.FLEET_CFG = _cfg_before
            wd.RUNNING.clear()
            for f in wd.RUNS.glob("*.json"):
                f.unlink()
    finally:
        Path.home = _real_home

    print("== schedule grace + success ==")
    import schedule as sched
    from datetime import datetime as _dt, timedelta as _td

    check("grace: a 20m job tolerates 10m (half the period)",
          sched.grace_seconds({"kind": "every", "every": "20m"}) == 600)
    check("grace: a 2m job is floored at 2 minutes, not 1",
          sched.grace_seconds({"kind": "every", "every": "2m"}) == sched.GRACE_MIN)
    check("grace: a daily job is capped at 2 hours, not half a day",
          sched.grace_seconds({"kind": "daily", "daily": "07:00"}) == sched.GRACE_MAX)
    check("grace: a one-shot gets the maximum (no next occurrence to fall back on)",
          sched.grace_seconds({"kind": "at", "at": "2026-08-01T09:00:00"}) == sched.GRACE_MAX)
    # The bug this catches: reading job["value"] instead of job["every"] returned
    # the floor for EVERY recurring job, quietly making the feature do nothing.
    check("grace: reads the kind's own key, so a 4h job really gets 2h",
          sched.grace_seconds({"kind": "every", "every": "4h"}) == sched.GRACE_MAX)

    sched.JOBS = SAND / "jobs.json"
    sched.SCHEDULE = SAND
    T0 = _dt(2026, 8, 1, 12, 0, 0)

    def _job(**kw):
        j = {"id": "j1", "kind": "every", "every": "20m", "to": "orchestrator",
             "text": "ping", "weekdays": False, "nextRun": None, "lastRun": None}
        j.update(kw); return [j]

    # within grace -> fires
    sched.save_jobs(_job(nextRun=(T0 - _td(minutes=5)).strftime(sched.FMT)))
    fire, kept = sched.due_jobs(T0)
    check("a job 5m late (grace 10m) fires", len(fire) == 1)
    check("firing does not count as missed", not kept[0].get("missed"))

    # beyond grace -> fast-forwards silently, but visibly counted
    sched.save_jobs(_job(nextRun=(T0 - _td(hours=3)).strftime(sched.FMT)))
    fire, kept = sched.due_jobs(T0)
    check("a job 3h late (grace 10m) does NOT fire", fire == [])
    check("the skip is counted, not silent", kept[0].get("missed") == 1)
    check("and it is rescheduled into the future",
          _dt.strptime(kept[0]["nextRun"], sched.FMT) > T0)

    # the laptop case the whole thing exists for
    sched.save_jobs(_job(kind="daily", daily="07:00", every=None,
                         nextRun=(T0.replace(hour=7, minute=0)).strftime(sched.FMT)))
    fire, _ = sched.due_jobs(T0.replace(hour=7, minute=40))
    check("a 07:00 briefing still fires at 07:40 (within 2h)", len(fire) == 1)
    sched.save_jobs(_job(kind="daily", daily="07:00", every=None,
                         nextRun=(T0.replace(hour=7, minute=0)).strftime(sched.FMT)))
    fire, _ = sched.due_jobs(T0.replace(hour=18, minute=0))
    check("the same briefing does NOT fire at 18:00", fire == [])

    # lastRun means "tried"; lastSuccess means "landed"
    jobs = _job(id="jx")
    sched.stamp_success(jobs, ["jx"], T0)
    check("stamp_success marks a delivered job", jobs[0]["lastSuccess"] == T0.strftime(sched.FMT))
    jobs2 = _job(id="jy")
    sched.stamp_success(jobs2, ["other"], T0)
    check("stamp_success leaves undelivered jobs alone", "lastSuccess" not in jobs2[0])
    wd_src3 = (real_root / "tools" / "discord" / "watchdog.py").read_text(encoding="utf-8")
    check("the watchdog stamps success only after the envelope write succeeded",
          "delivered.append(job.get(\"id\"))" in wd_src3
          and wd_src3.index("delivered.append") < wd_src3.index("stamp_success"))

    # == config\ — the single reader ==========================================
    # Settings used to live in four places and three formats. The rules that
    # matter are: env beats file beats default, a broken file can never take
    # the fleet down, and a credential is named here but stored in .env.
    print("== config loader ==")
    import omnius_config as _oc
    _cfgdir = SAND / "config"
    _cfgdir.mkdir(parents=True, exist_ok=True)
    _real_cfgdir, _oc.CONFIG_DIR = _oc.CONFIG_DIR, _cfgdir
    _real_root_oc, _oc.ROOT = _oc.ROOT, SAND
    try:
        check("no config folder at all is a working state, not an error",
              _oc.load("nothing") == {} and _oc.get({}, "s", "k", None, "fallback") == "fallback")

        (_cfgdir / "demo.ini").write_text(
            "[demo]\nport = 6000\nflag = yes\nname = from-file\n", encoding="utf-8")
        _demo = _oc.load("demo")
        check("a value is read from the file", _oc.get(_demo, "demo", "name") == "from-file")
        check("...typed readers parse it", _oc.get_int(_demo, "demo", "port", None, 1) == 6000
              and _oc.get_bool(_demo, "demo", "flag", None, False) is True)
        _os0.environ["OMNIUS_TEST_NAME"] = "from-env"
        check("an environment variable BEATS the file (the one-off override)",
              _oc.get(_demo, "demo", "name", "OMNIUS_TEST_NAME") == "from-env")
        del _os0.environ["OMNIUS_TEST_NAME"]
        check("...and the default is used when neither is set",
              _oc.get(_demo, "demo", "missing", "OMNIUS_TEST_ABSENT", "d") == "d")

        # A typo in a config file must never stand between him and a working
        # fleet: report it, use the default, keep going. Same rule fleet.json
        # already follows ("broken fleet.json must never stop a spawn").
        (_cfgdir / "broken.ini").write_text("[demo\nthis is not ini at all\n", encoding="utf-8")
        _oc._problems.clear()
        check("a CORRUPT config file falls back instead of raising",
              _oc.load("broken") == {})
        check("...and says so, so it is fixable rather than mysterious",
              any("broken.ini" in p for p in _oc.problems()))
        (_cfgdir / "demo.ini").write_text("[demo]\nport = twelve\n", encoding="utf-8")
        _oc._problems.clear()
        check("a non-numeric value falls back to the default, never crashes",
              _oc.get_int(_oc.load("demo"), "demo", "port", None, 5111) == 5111)

        # The multi-account shape, decided 2026-08-05 (he has several mailboxes).
        (_cfgdir / "acc.ini").write_text(
            "[email]\ndefault = work\n\n[account.work]\nuser = a@example.com\n"
            "password_env = TEST_SECRET_KEY\n\n[account.personal]\nuser = b@example.com\n",
            encoding="utf-8")
        _acc = _oc.group(_oc.load("acc"), "account")
        check("N accounts are read without a code change",
              sorted(_acc) == ["personal", "work"] and _acc["work"]["user"] == "a@example.com")

        # THE rule that lets config\ be readable and shareable: the credential
        # is NAMED here and STORED in .env (config\README.md, rule 2).
        (SAND / ".env").write_text("TEST_SECRET_KEY=hunter2\n", encoding="utf-8")
        _val, _key = _oc.secret(_acc["work"], env=_oc.load_env(SAND))
        check("a secret is resolved out of .env by the name config gives",
              _val == "hunter2" and _key == "TEST_SECRET_KEY")
        check("an account with no password_env resolves to nothing, not a crash",
              _oc.secret(_acc["personal"]) == ("", ""))
        check("config files never hold the credential itself",
              "hunter2" not in (_cfgdir / "acc.ini").read_text(encoding="utf-8"))

        # Optional capabilities (image / video / tts). roadmap.md's rule:
        # absence disables ONE capability with a clear message and never
        # breaks anything else, so a machine with no keys runs as well as one
        # with all of them.
        check("with no ai.ini at all, every capability is simply off",
              [s for _n, _p, _k, s in _oc.capability_status()] == ["off"] * 4)
        (_cfgdir / "ai.ini").write_text(
            "[image]\nprovider = nano-banana\napi_key_env = TEST_AI_KEY\n\n"
            "[video]\nprovider = veo-3.1-fast\napi_key_env = MISSING_KEY\n\n"
            "[tts]\nprovider = windows\n", encoding="utf-8")
        (SAND / ".env").write_text("TEST_SECRET_KEY=hunter2\nTEST_AI_KEY=abc\n",
                                   encoding="utf-8")
        _caps = {n: (p, k, s) for n, p, k, s in _oc.capability_status()}
        check("a provider WITH its key is ready", _caps["image"][2] == "ready")
        check("a provider whose key is missing reports NO KEY, not ready",
              _caps["video"][2] == "NO KEY")
        check("...and says so as a problem, since the file looks enabled",
              any("video stays off" in p for p in _oc.problems()))
        check("'windows' speech needs no key at all and is still ready",
              _caps["tts"] == ("windows", "", "ready"),
              "the built-in SAPI5 voices are free and offline")
        _cap_desc = _oc.describe()
        check("!config lists capabilities without ever printing a key",
              "nano-banana" in _cap_desc and "abc" not in _cap_desc)
    finally:
        _oc.CONFIG_DIR, _oc.ROOT = _real_cfgdir, _real_root_oc
        _oc._problems.clear()

    # !config is READ ONLY and must never print a value. His call 2026-08-05:
    # readable from Discord, edited at the desk - a phone typo with nobody at
    # the keyboard is the failure that cannot be undone remotely.
    _desc = _oc.describe()
    _real_secrets = [v for v in (api.TOKEN, api.GUILD, api.OWNER) if v]
    check("!config reports secrets as set/NOT SET, never their value",
          not any(s in _desc for s in _real_secrets)
          and ("set" in _desc or "NOT SET" in _desc))
    check("!config says WHERE each value came from, not just what it is",
          "default" in _desc or "config\\" in _desc or "env " in _desc)
    check("!config is registered as a control command",
          "!config" in wd.CONTROL_COMMANDS)
    check("there is no !config SET verb (read-only by design)",
          '"!config set"' not in _wsrc and "!config-set" not in _wsrc)

    # The move itself: one place to look, but nothing may break mid-migration.
    # Asserted on the RESOLVER, not on where it happens to point here: config\*
    # is gitignored, so on a fresh clone there is no config\fleet.json yet and
    # the resolver correctly falls back - which used to fail this check and
    # made the suite look broken on the one tree nobody has installed yet.
    _fcfg_body = _wsrc[_wsrc.index("def _fleet_cfg"):]
    _fcfg_body = _fcfg_body[:_fcfg_body.index("\ndef ", 10)]
    check("fleet.json is read from config\\, falling back to the old root path",
          '"config" / "fleet.json"' in _fcfg_body and 'ROOT / "fleet.json"' in _fcfg_body)
    _daybook_src = (_rr / "daybook" / "app.py").read_text(encoding="utf-8")
    check("the notes app prefers config\\notes.ini but still boots standalone",
          '"config" / "notes.ini"' in _daybook_src and 'BASE_DIR / "config.ini"' in _daybook_src)
    # The invariant is "it still boots outside an Omnius tree", NOT "it never
    # names the module". The Settings page needs config, so the import exists -
    # but lazily, inside a guarded function, so a copy of daybook\ on its own
    # still serves notes and simply reports that settings are unavailable.
    import ast as _dbast
    _db_top = {a.name for n in _dbast.parse(_daybook_src).body
               if isinstance(n, _dbast.Import) for a in n.names}
    check("the notes app never imports tools\\ at MODULE level (stays standalone)",
          "omnius_config" not in _db_top,
          "a top-level import would make daybook unusable outside a workspace")
    check("...it reads config lazily and degrades when there is no workspace",
          "def omnius_settings" in _daybook_src
          and '"available": False' in _daybook_src)
    check("the settings page is READ-only — no write route exists",
          "/api/config" in _daybook_src and "/api/config/set" not in _daybook_src)
    _api_src = (_rr / "tools" / "discord" / "api.py").read_text(encoding="utf-8")
    check("api.py delegates .env parsing instead of keeping a second parser",
          "omnius_config.load_env" in _api_src)
    check("...but keeps a fallback, because nothing is fixable once Discord is down",
          "except Exception" in _api_src and "env = {}" in _api_src)

    # config\ holds addresses and hostnames: identifying, though not secret. It
    # must travel in his backup and never reach a release.
    _gitignore = (_rr / ".gitignore").read_text(encoding="utf-8")
    check("real config is gitignored, only the templates are not",
          "config/*" in _gitignore and "!config/*.example.ini" in _gitignore)
    # fleet.json was on the tracked side until 2026-08-14 and should never have
    # been: its desks map is keyed by SESSION ID, so a repo whose only job is
    # installing a stranger a fresh Omnius carried a real session id.
    check("...and fleet.json is NOT one of them - it names his projects",
          "!config/fleet.json" not in _gitignore
          and "!config/fleet.example.json" in _gitignore)
    _pack = (_rr / "pack.ps1").read_text(encoding="utf-8")
    check("-Fresh drops the live config files but ships the templates",
          "config/$($_.Name)" in _pack and "*.example." in _pack)
    check("...and no longer keeps fleet.json by name",
          "$configKeep = @('README.md')" in _pack)
    _inst_cfg = (_rr / "install.ps1").read_text(encoding="utf-8")
    check("install seeds every example, .json as well as .ini",
          "'*.example.*'" in _inst_cfg,
          "fleet.json is no longer tracked, so something must create it")
    # The rule must be deny-by-default. It was `-Filter '*.ini'` until
    # routines.json landed in config\ on 2026-08-07 - a .json, so the filter
    # never saw it, and a routine's text can name an account. Applying the
    # script's OWN allow-list to the REAL folder, so a new file added here
    # fails this test instead of shipping to a stranger.
    # fleet.json ships (a fresh machine must spawn the right thing on day one)
    # but its `desks` map is INSTANCE STATE: keys are session ids, i.e. project
    # names. One `!restart max` on a project desk wrote a real key and the
    # release refused (2026-08-10) - the guard was right, the file was wrong.
    _san = (_rr / "tools" / "release_sanitize.py").read_text(encoding="utf-8")
    check("-Fresh empties fleet.json's per-desk overrides",
          "def strip_fleet_desks" in _san and 'k.startswith("_")' in _san,
          "session ids carry project names")
    check("...while keeping defaults, roles and the explaining _comment",
          '"desks"' in _san and "keep" in _san)
    check("...and it runs before the audit, so a leak still refuses the build",
          _san.index("strip_fleet_desks(zip_path)") < _san.index("bad = audit(zip_path)"))
    # The EXAMPLE is what ships: fleet.json itself stopped being tracked on
    # 2026-08-14 (it names his desks), so install copies it from here.
    _fj = (_rr / "config" / "fleet.example.json").read_text(encoding="utf-8")
    check("the shipped fleet.json example uses a generic project name",
          "my-project.app" in _fj)
    # pack.bat --help silently built a 31MB backup and overwrote the day's zip.
    check("pack.ps1 refuses unknown arguments instead of packing anyway",
          "unknown argument" in _pack and "$args.Count" in _pack,
          "a typo must not be indistinguishable from the default action")
    check("...and has a -Help that explains the three artefacts",
          "-Help" in _pack and "CLEAN RELEASE for a NEW instance" in _pack)

    check("-Fresh selects config by allow-list, not by an .ini filter",
          "$configKeep" in _pack and "-Filter '*.ini'" not in _pack)
    _m = re.search(r"\$configKeep\s*=\s*@\(([^)]*)\)", _pack)
    check("...and the allow-list is parseable", bool(_m))
    if _m:
        _keep = set(re.findall(r"'([^']+)'", _m.group(1)))
        # The real folder PLUS the instance files that only exist once someone
        # has used this install. config\* is gitignored, so on a clone none of
        # them are there - and a rule tested only against files that happen to
        # be present is not tested at all (2026-08-14: this passed for months
        # and failed the moment it met a fresh tree).
        _live = {f.name for f in (_rr / "config").iterdir() if f.is_file()}
        _live |= {"routines.json", "fleet.json", "allow-learned.json",
                  "omnius.ini", "email.ini", "guests.ini", "audit-sentinels.txt"}
        _ships = {n for n in _live if n in _keep or ".example." in n}
        _dropped = _live - _ships
        check("...so routines.json never reaches a release",
              "routines.json" in _dropped, f"ships: {sorted(_ships)}")
        check("...while every template and the README still do",
              {"README.md", "fleet.example.json"} <= _ships
              and all(".example." in n or n in _keep for n in _ships))
        check("...and the REAL fleet.json is dropped with the rest",
              "fleet.json" in _dropped,
              "its desks map is keyed by session id, i.e. by his project names")
        check("...and nothing live slips through",
              not any(n.endswith(".ini") and not n.endswith(".example.ini")
                      for n in _ships), f"ships: {sorted(_ships)}")
    _sanitize = (_rr / "tools" / "release_sanitize.py").read_text(encoding="utf-8")
    check("the release audit can even SEE .ini files (an unlisted suffix is unchecked)",
          '".ini"' in _sanitize)
    import release_sanitize as _rsan
    _mailpat = _rsan.IDENTIFYING.get("email address")
    check("a real address in a shipped file refuses the release",
          bool(_mailpat and _mailpat.search("someone@somecompany.com")))
    check("...while RFC 2606 documentation addresses do not (templates must ship)",
          bool(_mailpat) and not _mailpat.search("you@example.com")
          and not _mailpat.search("smtp_host = smtp.gmail.com"))
    for _tpl in ("README.md", "omnius.example.ini", "notes.example.ini", "email.example.ini"):
        _p = _rr / "config" / _tpl
        check(f"config\\{_tpl} ships and is free of identifying data",
              _p.is_file() and not any(pat.search(_p.read_text(encoding="utf-8"))
                                       for pat in _rsan.IDENTIFYING.values()))

    print("== memory budget ==")
    MEM = real_root / "memory"

    def _mem_file(rel):
        """This instance's memory file, or the seed that ships in its place.

        memory\\ is the instance's biography and is gitignored (2026-08-13), so
        a clone has none until install seeds it from templates\\fresh\\memory\\.
        Checks about what memory SAYS therefore have to accept the seed, or
        they only pass on the machine that wrote them - which is how a public
        repo ends up with a suite nobody else can run (2026-08-14).
        """
        live = MEM / rel
        return live if live.is_file() else real_root / "templates" / "fresh" / "memory" / rel
    BUDGETS = {
        "orchestrator/status.md": 9_000,      # read at EVERY boot - the expensive one
        "orchestrator/MEMORY.md": 3_000,      # an index; if it grows it is not an index
        "shared/MEMORY.md": 4_000,
        "shared/USER.md": 6_000,
    }
    TOPIC_BUDGET = 13_000
    ALWAYS_READ_BUDGET = 16_000               # index + status + shared, per session

    for rel, cap in BUDGETS.items():
        p_ = MEM / rel
        if not p_.is_file():
            continue
        size = len(p_.read_text(encoding="utf-8"))
        check(f"memory budget: {rel} <= {cap:,} chars (is {size:,})", size <= cap,
              "move the oldest non-current content into a topic file")

    for p_ in sorted((MEM / "orchestrator" / "topics").glob("*.md")):
        size = len(p_.read_text(encoding="utf-8"))
        check(f"memory budget: topics/{p_.name} <= {TOPIC_BUDGET:,} chars (is {size:,})",
              size <= TOPIC_BUDGET, "split the topic rather than growing it")

    # == !model: model/effort from Discord ====================================
    # Model and effort are pinned at a run's LAUNCH - nothing changes them
    # mid-run, and /effort refuses to move a launch pin. So the only honest
    # remote path is "change the config, then start a new run", which is what
    # this verb automates. The sanctioned manual path was editing fleet.json by
    # hand; this is the same write, from a phone.
    print("== !model ==")
    _fleet_real = wd.FLEET_CFG
    wd.FLEET_CFG = SAND / "fleet.json"
    wd.FLEET_CFG.write_text(json.dumps({
        "_comment": "must survive a rewrite",
        "defaults": {"model": "opus", "effort": "xhigh"},
        "roles": {"project": {"_why": "must survive a rewrite"}},
        "desks": {"_comment": "must survive a rewrite"}}, indent=2), encoding="utf-8")
    _ra, _sa = wd.run_active, wd.session_alive
    wd.run_active = lambda s: False
    wd.session_alive = lambda s: False
    _sent = []
    _real_send = api.send_message
    api.send_message = lambda c, t, files=None: _sent.append(t)

    class _Desk:
        session, channel_name = "demo-app.app", "app"

    def _model(cmd):
        _sent.clear()
        wd.handle_control(cmd, "C1", _Desk(), {})
        return _sent[0] if _sent else ""

    try:
        out = _model("!model")
        check("!model with no args reports model AND effort",
              "opus" in out and "xhigh" in out)
        check("...and says WHERE each came from", "defaults" in out)
        check("...and says nothing is running, so this is the NEXT run's setting",
              "nothing running" in out)

        # RUNNING vs CONFIGURED. Model/effort are pinned at launch, so a change
        # made during a run is real but not yet in effect. Reporting only the
        # config would claim otherwise - and the desk would keep burning the
        # old model while Discord said it was on the new one.
        _rl = wd.read_lease
        wd.run_active = lambda s: True
        wd.read_lease = lambda s: {"session": s, "pid": 1, "model": "opus", "effort": "xhigh"}
        check("running_model reads what the LIVE run launched on",
              wd.running_model("demo-app.app") == ("opus", "xhigh"))
        out = _model("!model")
        check("!model shows what is running right now", "running now" in out)
        wd.fleet_set_desk("demo-app.app", model="sonnet", effort="low")
        out = _model("!model")
        check("a change made mid-run is flagged as NOT yet in effect",
              "config differs" in out and "next" in out.lower())
        check("...and it still reports the live run truthfully", "opus" in out)
        wd.read_lease = lambda s: {"session": s, "pid": 1}       # pre-upgrade lease
        check("a lease with no model recorded says so instead of guessing",
              wd.running_model("demo-app.app") is None
              and "before the watchdog recorded" in _model("!model"))
        # The ALIAS is what we ask for; the resolved id is what answered.
        # "opus" does not say WHICH Opus, and that was his actual question.
        _hd = wd.history_dir_for
        _proj = SAND / "hist"
        _proj.mkdir(parents=True, exist_ok=True)
        (_proj / "s.jsonl").write_text(
            json.dumps({"message": {"model": "claude-opus-5", "usage": {}}}) + "\n"
            + json.dumps({"message": {"model": "<synthetic>", "usage": {}}}) + "\n",
            encoding="utf-8")
        wd.history_dir_for = lambda cwd: _proj
        try:
            check("resolved_model reads the id Claude actually used",
                  wd.resolved_model("demo-app.app") == "claude-opus-5")
            check("...ignoring <synthetic> entries, which name no real model",
                  wd.resolved_model("demo-app.app") != "<synthetic>")
            check("!model shows what the alias resolved to",
                  "claude-opus-5" in _model("!model"))
            wd.history_dir_for = lambda cwd: SAND / "no-such-history"
            check("an unknown desk reports None rather than guessing the config",
                  wd.resolved_model("demo-app.app") is None,
                  "for a question about WHICH model ran, a guess is the worst answer")
            check("...and !model simply omits it rather than inventing one",
                  "claude-opus-5" not in _model("!model"))
        finally:
            wd.history_dir_for = _hd

        check("fmt_model parenthesises the config when nothing is running",
              (lambda: (setattr(wd, "run_active", lambda s: False),
                        wd.fmt_model("demo-app.app").startswith("("))[1])())
        wd.read_lease = _rl
        wd.run_active = lambda s: False
        wd.fleet_set_desk("demo-app.app", clear=True)

        _model("!model sonnet low")
        d = wd.desk_config("demo-app.app")
        check("!model sonnet low sets both", d["model"] == "sonnet" and d["effort"] == "low")
        check("...and it is written to fleet.json, so it travels and persists",
              json.loads(wd.FLEET_CFG.read_text(encoding="utf-8"))
                  ["desks"]["demo-app.app"] == {"model": "sonnet", "effort": "low"})
        check("...and now reports 'this desk' as the source",
              "this desk" in _model("!model"))

        _model("!model effort high")
        d = wd.desk_config("demo-app.app")
        check("!model effort high changes effort WITHOUT touching model",
              d["effort"] == "high" and d["model"] == "sonnet")

        out = _model("!model sonnet turbo")
        check("a bad effort is refused, and lists the real ones",
              "not an effort" in out and "xhigh" in out)
        check("...and nothing was written", wd.desk_config("demo-app.app")["effort"] == "high")

        out = _model("!model gpt-5")
        check("an unrecognised model is accepted but WARNED about",
              "don't recognise" in out and wd.desk_config("demo-app.app")["model"] == "gpt-5",
              "never silently: a typo would surface as a failed run much later")
        check("a real model id is not warned about", "recognise" not in _model("!model claude-opus-5"))

        _model("!model reset")
        d = wd.desk_config("demo-app.app")
        check("!model reset falls back to the inherited default",
              d["model"] == "opus" and d["effort"] == "xhigh")
        check("...and removes the desk key rather than leaving an empty one",
              "demo-app.app" not in json.loads(wd.FLEET_CFG.read_text(encoding="utf-8"))["desks"])

        cfg_after = json.loads(wd.FLEET_CFG.read_text(encoding="utf-8"))
        check("hand-written _comment/_why keys SURVIVE the rewrite",
              cfg_after["_comment"] == "must survive a rewrite"
              and cfg_after["roles"]["project"]["_why"] == "must survive a rewrite",
              "fleet.json documents hard-won decisions; a template rewrite would erase them")
        check("the file keeps its trailing newline (it is tracked; no diff noise)",
              wd.FLEET_CFG.read_text(encoding="utf-8").endswith("}\n"))

        wd.run_active = lambda s: True
        check("with a run in flight it says the change lands on the NEXT run",
              "next one" in _model("!model sonnet"))
        wd.run_active = lambda s: False

        class _NoDesk:
            session, channel_name = None, "alerts"
        _sent.clear()
        wd.handle_control("!model sonnet", "C1", _NoDesk(), {})
        check("a channel with no desk is told so, not silently ignored",
              _sent and "no desk" in _sent[0])
        check("!model is a control command (answered with no desk spawn)",
              "!model" in wd.CONTROL_COMMANDS)

        # !restart <model> [effort] - set AND cut over in one command, because
        # "change a running desk's model" always means "restart it on the new
        # one" anyway.
        _ks, _sr = wd.kill_session, wd.start_run
        started = []
        wd.kill_session = lambda s: "killed"
        wd.start_run = lambda s, model=None, effort=None: started.append((s, model, effort)) or True
        try:
            out = _model("!restart haiku medium")
            d = wd.desk_config("demo-app.app")
            check("!restart <model> <effort> applies the change and restarts",
                  d["model"] == "haiku" and d["effort"] == "medium" and len(started) == 1)
            check("...and the reply names what it came back on",
                  "haiku" in out and "medium" in out)
            check("...and it PERSISTS, so the next run does not revert",
                  json.loads(wd.FLEET_CFG.read_text(encoding="utf-8"))
                      ["desks"]["demo-app.app"]["model"] == "haiku")
            out = _model("!restart bogus-effort-word extreme")
            check("a bad effort blocks the restart entirely",
                  "not an effort" in out and len(started) == 1,
                  "must not kill a desk and then refuse to configure it")
            started.clear()
            _model("!restart")
            check("bare !restart still just restarts, changing nothing",
                  len(started) == 1 and wd.desk_config("demo-app.app")["model"] == "haiku")
        finally:
            wd.kill_session, wd.start_run = _ks, _sr
        _model("!model reset")
    finally:
        api.send_message = _real_send
        wd.run_active, wd.session_alive = _ra, _sa
        wd.FLEET_CFG = _fleet_real

    # Today's live shape (2026-08-16): he put THIS desk on fable/max with
    # !model. That lands in desks["orchestrator"], which overlays role:
    # orchestrator - it must change model and effort and NOTHING else, or the
    # model choice silently costs the orchestrator its fresh-boot policy (the
    # 200x context-tax fix) the day it happens.
    print("== !model on the orchestrator keeps the role policy ==")
    _fleet_real2 = wd.FLEET_CFG
    wd.FLEET_CFG = SAND / "fleet-orch.json"
    wd.FLEET_CFG.write_text(json.dumps({
        "defaults": {"model": "opus", "effort": "xhigh", "resume": "transcript",
                     "permissionMode": None, "window": "terminal"},
        "roles": {"orchestrator": {"resume": "fresh"}},
        "desks": {}}, indent=2) + "\n", encoding="utf-8")
    try:
        wd.fleet_set_desk("orchestrator", model="fable", effort="max")
        _ocfg = wd.desk_config("orchestrator")
        check("the override lands in desks[orchestrator]",
              json.loads(wd.FLEET_CFG.read_text(encoding="utf-8"))
                  ["desks"].get("orchestrator", {}).get("model") == "fable")
        check("desk_config resolves the overridden model/effort",
              _ocfg["model"] == "fable" and _ocfg["effort"] == "max")
        check("...and resume stays 'fresh' from the role",
              _ocfg["resume"] == "fresh",
              "a model override must never drag the 11 MB transcript back")
        check("...and the permission profile survives too",
              _ocfg["permissionMode"] is None)
        wd.fleet_set_desk("orchestrator", clear=True)
        check("reset returns the desk to the pure role/default stack",
              wd.desk_config("orchestrator")["model"] == "opus"
              and wd.desk_config("orchestrator")["resume"] == "fresh")
    finally:
        wd.FLEET_CFG = _fleet_real2

    # == BOM: an invisible byte that empties every setting ====================
    # configparser rejects a whole .ini with MissingSectionHeaderError if it
    # starts with a BOM - so every value silently reverts to its default. BOMs
    # arrive by accident on Windows constantly: Notepad writes one, and
    # PowerShell 5.1's `Set-Content -Encoding utf8` cannot NOT write one, which
    # is how our own installer produced them (caught 2026-08-11, pre-ship).
    print("== config encoding ==")
    import omnius_config as _oc                                   # noqa: E402
    _cd = _oc.CONFIG_DIR
    try:
        _tmp = SAND / "cfgenc"; _tmp.mkdir(exist_ok=True)
        _oc.CONFIG_DIR = _tmp
        (_tmp / "omnius.ini").write_bytes("﻿[omnius]\nlanguage = español\n".encode("utf-8"))
        check("a BOM-prefixed .ini still parses",
              dict(_oc.load("omnius").get("omnius") or {}).get("language") == "español",
              "utf-8-sig, not utf-8 - otherwise every setting is silently lost")
        (_tmp / "omnius.ini").write_bytes(b"[omnius]\nlanguage = deutsch\n")
        check("...and a plain UTF-8 one is unaffected",
              dict(_oc.load("omnius").get("omnius") or {}).get("language") == "deutsch")
    finally:
        _oc.CONFIG_DIR = _cd
    _inst0 = (real_root / "install.ps1").read_text(encoding="utf-8")
    check("install.ps1 writes config WITHOUT a BOM",
          "Write-Utf8NoBom" in _inst0
          and "Set-Content $iniPath" not in _inst0
          and "Set-Content $userMd" not in _inst0,
          "PowerShell 5.1 Set-Content -Encoding utf8 always writes one")

    # == a restored workspace must not be a silently dead fleet ==============
    # 2026-08-14, the move to the permanent PC. From 13:03 to 14:05 not one
    # desk could start - shutil.which("claude") returned None inside the service
    # while claude.exe sat in %USERPROFILE%\.local\bin and that folder WAS in
    # the persisted user PATH. The service had started before the installer
    # wrote it, and a Windows process never re-reads its environment. Every
    # health signal read green: beacon 3s old, gateway connected, 14 channels,
    # "autostart healthy". Two of his messages and the daily briefing were lost
    # to a backoff nobody could see.
    print("== stale environment: the fleet that cannot spawn ==")
    _which, _cx, _cw = wd.shutil.which, wd._claude_exe, wd._claude_warned
    try:
        _fake = SAND / "fakebin"; _fake.mkdir(exist_ok=True)
        _exe = _fake / "claude.exe"; _exe.write_text("", encoding="utf-8")
        wd._claude_exe = None
        wd.shutil.which = lambda n: "C:\\on\\path\\claude.exe"
        check("claude_exe uses PATH when PATH works",
              wd.claude_exe(recheck=True) == "C:\\on\\path\\claude.exe")

        wd._claude_exe, wd._claude_warned = None, False
        wd.shutil.which = lambda n: None
        _home = wd.os.path.expanduser
        try:
            wd.os.path.expanduser = lambda p: str(_fake.parent) if p == "~" else _home(p)
            (_fake.parent / ".local" / "bin").mkdir(parents=True, exist_ok=True)
            (_fake.parent / ".local" / "bin" / "claude.exe").write_text("", encoding="utf-8")
            _got = wd.claude_exe(recheck=True)
            check("...and falls back to the known install location when PATH is stale",
                  _got is not None and _got.endswith("claude.exe"),
                  "this is the whole 2026-08-14 outage")
            check("...and SAYS its environment is out of date, so it can be fixed",
                  wd._claude_warned is True,
                  "working-but-stale must not be silent; it wants a restart")
        finally:
            wd.os.path.expanduser = _home

        wd._claude_exe = None
        wd.shutil.which = lambda n: None
        _cfg = wd.OMNIUS_CFG
        try:
            wd.OMNIUS_CFG = {"fleet": {"claude_path": str(_exe)}}
            check("...and an explicit [fleet] claude_path wins over everything",
                  wd.claude_exe(recheck=True) == str(_exe),
                  "the escape hatch when a machine puts it somewhere odd")
        finally:
            wd.OMNIUS_CFG = _cfg
    finally:
        wd.shutil.which, wd._claude_exe, wd._claude_warned = _which, _cx, _cw

    _wd_now = (real_root / "tools" / "discord" / "watchdog.py").read_text(encoding="utf-8")
    check("the beacon publishes what the SERVICE can see, not what a shell can",
          '"claude": claude_exe()' in _wd_now,
          "the process is the only authority on its own environment")
    check("a fleet that cannot spawn ALERTS him, once",
          "def alert_no_cli" in _wd_now and "_no_cli_alerted" in _wd_now,
          "he should not learn 'the fleet is deaf' from a log he never opens")
    check("...and start_run calls it rather than only logging",
          "alert_no_cli()" in _wd_now.split("def start_run")[1][:4000])
    check("startup RETRIES a network that is not up yet",
          "network not up yet" in _wd_now and "STARTUP_NET_ATTEMPTS" in _wd_now,
          "a restored PC boots, joins wifi, then starts services - in that order")
    check("...but still fails fast on a bad token, which retrying cannot fix",
          "transient" in _wd_now)

    _auto = (real_root / "tools" / "discord" / "autostart.ps1").read_text(encoding="utf-8")
    check("autostart -Action status FAILS when the service cannot find the CLI",
          "cannot find the claude CLI" in _auto,
          "a green status that hides a dead fleet is worse than no status")
    check("...and repair restarts the task, since the task DEFINITION was fine",
          "restarting it - its environment predates the CLI install" in _auto,
          "Get-Drift only compares the definition on disk, so repair was a no-op")
    check("install.ps1 restarts the services after it edits PATH",
          "so it can see the new PATH" in _inst0 or
          "so it can see the new PATH" in (real_root / "install.ps1").read_text(encoding="utf-8"),
          "a running service keeps the environment it was born with")
    _inst_now = (real_root / "install.ps1").read_text(encoding="utf-8")
    # Every script-scoped flag must be initialised at the top, not only where
    # it is written. PathChanged was set solely inside Add-ClaudeToUserPath,
    # which does not run when the CLI is already present - so every normal
    # install then READ an unset variable, fatal under StrictMode. The release
    # gate refused the build, which is precisely why that gate exists.
    for _v in sorted(set(re.findall(r"\$script:(\w+)", _inst_now))):
        _init = re.search(rf"^\$script:{_v}\s*=", _inst_now, re.M)
        check(f"install.ps1 initialises $script:{_v} at top level",
              _init is not None,
              "an unset script variable is fatal the moment StrictMode is on")
    check("install.ps1 catches a .env carried over from the old machine",
          "MACHINE_NAME" in _inst_now and "COMPUTERNAME" in _inst_now,
          ".env cannot travel in the zip, so it is hand-copied - with its old "
          "machine name, which then makes every claim read as FOREIGN")
    check("...and names the command that clears the stale claims",
          "fleet_ops.py status --prune" in _inst_now)

    # == the shipped demo project + MIT ======================================
    # A public repo teaches by example: three desks (back, front, auditor)
    # around a deliberately tension-laden brief. The auditor being READ-ONLY
    # is the load-bearing idea - findings go to the owner, never silent fixes.
    print("== demo project & license ==")
    _demo = real_root / "templates" / "demo-project"
    check("the demo project template ships", _demo.is_dir())
    for _d in ("back", "front", "auditor"):
        check(f"...with a {_d} desk role card", (_demo / _d / "CLAUDE.md").is_file())
    _aud = (_demo / "auditor" / "CLAUDE.md").read_text(encoding="utf-8") \
        if (_demo / "auditor" / "CLAUDE.md").is_file() else ""
    check("the auditor is told it is read-only, in its own role card",
          "read-only" in _aud and "never fix" in _aud,
          "an auditor that fixes destroys the who-knew-what trail")
    check("the brief exists and plants the tensions the auditor hunts",
          (_demo / "memory" / "project-brief.md").is_file()
          and "hostile" in (_demo / "front" / "CLAUDE.md").read_text(encoding="utf-8"))
    _fo_src = (real_root / "tools" / "orchestrator" / "fleet_ops.py").read_text(encoding="utf-8")
    check("fleet_ops new-project accepts --template, so the demo stamps through "
          "the real machinery", '"--template"' in _fo_src and "template=args.template" in _fo_src)
    check("LICENSE exists and is MIT",
          (real_root / "LICENSE").is_file()
          and "MIT License" in (real_root / "LICENSE").read_text(encoding="utf-8"))
    check("...and the README carries the license badge",
          "license-MIT" in (real_root / "README.md").read_text(encoding="utf-8"))

    # == the one-line installer ==============================================
    # get.ps1 is `iex`'d into a STRANGER'S session. The 2026-08-11 lesson ran
    # the other way - a third-party installer left StrictMode on in OUR session
    # and the next line died - so ours must leave nothing behind, and must
    # never install over an existing folder (that is somebody's instance).
    print("== the one-line installer (get.ps1) ==")
    _get = (real_root / "get.ps1").read_text(encoding="utf-8")
    check("get.ps1 exists and README hands out its one-liner",
          "get.ps1 | iex" in (real_root / "README.md").read_text(encoding="utf-8"))
    check("it never sets StrictMode in the caller's session",
          "Set-StrictMode" not in _get.replace("no Set-StrictMode", ""),
          "iex runs in THEIR scope; state we set is state they keep")
    check("...and preferences are function-local, not script-scope",
          "function Install-Omnius" in _get
          and _get.index("$ErrorActionPreference = 'Stop'")
          > _get.index("function Install-Omnius"))
    check("it refuses a target folder that already has content",
          "already exists and is not empty" in _get,
          "an existing folder is somebody's instance - memory, notes, projects")
    check("...and a prompt it cannot ask fails SAFE, not into an install",
          "no interactive console" in _get)

    # == the two ways a prerequisite install failed on the new laptop =========
    # 2026-08-14, a real move to a clean Windows 11. Neither is caught by the
    # release gate, because that runs install.ps1 -CheckOnly and Ask-Install
    # returns $false there - so the install PATHS have never once executed
    # under test. These read the source instead; a weak check beats none on
    # the one script every new machine depends on.
    print("== installer: prerequisites on a clean machine ==")
    _ilines = _inst0.splitlines()
    _unsourced = []
    for _i, _l in enumerate(_ilines):
        if "winget install" not in _l or _l.strip().startswith("#"):
            continue
        # the call may wrap with a backtick continuation
        if "--source winget" not in " ".join(_ilines[_i:_i + 3]):
            _unsourced.append(_i + 1)
    check("every winget install names --source winget",
          not _unsourced, f"unsourced at line(s) {_unsourced} — msstore failed TLS "
          "(0x8a15005e); winget then called the package ambiguous and installed "
          "NOTHING, so node and ffmpeg both died in one run")
    # The Claude installer's own closing note: "Native installation exists but
    # C:\\Users\\<you>\\.local\\bin is not in your PATH. Add it by opening:
    # System Properties -> Environment Variables ..." Making him click through
    # that is exactly what this installer exists to remove.
    check("install.ps1 adds Claude's bin to the user PATH itself",
          "Add-ClaudeToUserPath" in _inst0 and "'Path', $new, 'User'" in _inst0)
    check("...and calls it right after running that installer",
          _inst0.index("Add-ClaudeToUserPath") < _inst0.index("function Add-ClaudeToUserPath")
          or "Add-ClaudeToUserPath\n    }" in _inst0)
    check("...appending, never replacing, the existing user PATH",
          "$user.TrimEnd(';') + ';' + $dir" in _inst0,
          "clobbering PATH on someone's work laptop is unrecoverable")
    check("...and is idempotent, so a second run does not stack duplicates",
          "already there" in _inst0)

    # == a machine without winget must still be installable ===================
    # 2026-08-14, run in a clean Windows Sandbox: no winget there, so the
    # installer printed "guided installs disabled", three "missing" lines and
    # "fix, then re-run" - an installer that could not install anything. The
    # Claude CLI in the same run installed fine from its own URL, which is the
    # proof that a package manager was never required.
    check("winget missing is not a dead end - there is a direct route",
          "Install-GitDirect" in _inst0 and "Install-PythonDirect" in _inst0
          and "Install-FfmpegDirect" in _inst0)
    check("...offered when winget is absent OR when winget ran and failed anyway",
          "if (-not $present -and $t['Direct'])" in _inst0,
          "winget called node and ffmpeg ambiguous and installed neither")
    for _src in ("https://www.python.org/ftp/python/",
                 "api.github.com/repos/git-for-windows/git/releases/latest",
                 "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"):
        check(f"...from the official source ({_src.split('/')[2]})", _src in _inst0)
    check("Python installs per-user, so it needs no administrator",
          "InstallAllUsers=0" in _inst0 and "PrependPath=1" in _inst0,
          "PrependPath is what makes `python` resolve afterwards")
    # THE hang of 2026-08-14: the py launcher installs for ALL USERS by default
    # even inside a per-user install, that needs elevation, and /quiet has no
    # way to ask - so the installer sat there silently, apparently forever.
    check("...including the py launcher, which otherwise waits on an elevation "
          "prompt it cannot show",
          "InstallLauncherAllUsers=0" in _inst0)
    # The slowest step in the install, and most of it is dead weight here: the
    # stdlib test suite, CHM docs, tkinter (nothing here draws a window), debug
    # binaries. Every skipped file is one Defender does not scan on write.
    check("...and skips the components nothing in this workspace uses",
          all(f"Include_{k}=0" in _inst0
              for k in ("test", "doc", "tcltk", "debug", "symbols", "dev")))
    # Include_dev=0 is only safe while every dependency ships a Windows wheel -
    # a wheel is a copy, not a build, so no headers are involved. A dependency
    # added here that has to compile would need them back.
    check("...which is only safe because nothing pip-installed builds from source",
          all(d in _inst0 for d in ("pywinpty", "psutil", "yt-dlp", "pymupdf",
                                    "faster-whisper", "playwright")))
    check("...while keeping pip itself, and PATH",
          "Include_pip=1" in _inst0 and "PrependPath=1" in _inst0)
    check("...and it runs /passive, so there is a visible progress bar",
          "'/passive'" in _inst0)
    # Silence for minutes is indistinguishable from a hang. Both halves matter:
    # bytes while downloading, elapsed time while an installer runs.
    check("downloads report megabytes instead of going silent",
          "MB downloaded" in _inst0 and "GetResponseStream" in _inst0,
          "PS 5.1's own progress bar makes a 100 MB download crawl")
    check("...and a running installer shows elapsed time",
          "function Wait-Installer" in _inst0 and "elapsed" in _inst0)
    # Both routes must agree, or two machines end up on different Pythons and a
    # wheel that installs on one fails on the other.
    _wid = re.search(r"WingetId='Python\.Python\.(3\.\d+)'", _inst0)
    _series = re.search(r"foreach \(\$series in @\('(3\.\d+)'", _inst0)
    check("the winget Python and the direct-download Python are the same series",
          bool(_wid) and bool(_series) and _wid.group(1) == _series.group(1),
          f"winget={_wid.group(1) if _wid else '?'} direct={_series.group(1) if _series else '?'}")
    # A series keeps getting SOURCE-only security releases after its last
    # Windows binary - 3.12 has four stacked on 3.12.10 - so a shallow probe
    # skips the whole series without saying so.
    check("...and the probe looks deep enough to see past source-only releases",
          "Select-Object -First 6" in _inst0)
    check("portable binaries land outside the workspace, so backups stay clean",
          "$env:LOCALAPPDATA 'Omnius\\bin'" in _inst0
          and "$script:OmniusBin" in _inst0)

    # == the CLI is installed LAST, and signing in is offered =================
    # His call, 2026-08-14: everything that can run unattended goes first, so
    # the long stretch of downloads needs nobody watching. The CLI ends in a
    # browser and a sign-in, and that is the one step a person must be there
    # for - so it is the one that waits until nothing else will interrupt.
    _order = [m.group(1) for m in re.finditer(r"@\{ Name='(\w+)';", _inst0)]
    check("the Claude CLI is the last prerequisite installed",
          _order and _order[-1] == 'claude', f"order: {_order}")
    check("...and git comes first, since everything else is cloned or checked out",
          _order and _order[0] == 'git', f"order: {_order}")
    # A CLI installs signed OUT, and every desk is a `claude -p` run: signed
    # out, the first Discord message fails with nothing in the channel to
    # explain why.
    check("install offers to sign in to Claude once the CLI is there",
          "claude auth login" in _inst0 and "function Test-ClaudeAuth" in _inst0)
    check("...reading the real state rather than assuming it",
          '"loggedIn"' in _inst0)
    check("...and staying silent when it cannot tell, instead of nagging",
          "cannot tell" in _inst0 and "return $null" in _inst0)
    check("...never forcing it - a browser sign-in needs a person present",
          "Ask-YesNo 'sign in now" in _inst0)
    # Defaults to NO because there is no clean way OUT of `claude auth login`
    # once it starts: Enter at its "Paste code here" prompt retries rather than
    # cancels, and on 2026-08-15 someone who changed their mind got five
    # "Invalid code" errors for it.
    check("...and defaults to no, since that flow cannot be cancelled once started",
          "-DefaultNo" in _inst0 and "[y/N]" in _inst0)
    check("...saying what will happen before it happens",
          "gives you a CODE to paste" in _inst0)

    # A workspace in Downloads is a real first-run footgun: people empty it, and
    # a GitHub zip unpacked there keeps its "-main" name. Every absolute hook
    # path, the shortcut and the git repo point at wherever this folder is.
    check("install warns when the workspace is somewhere it should not live",
          "Downloads" in _inst0 and "-main$|-master$" in _inst0
          and "OneDrive" in _inst0)

    # The one-liner and pack.ps1 are two halves of one contract, maintained
    # apart: get.ps1 hardcoded the zip's inner folder as 'omnius' while pack
    # names it after the repo, so the FIRST public release was downloaded and
    # then refused by the very script the README advertises (2026-08-15).
    # The archive's identity is install.bat, never a folder name.
    _get = (real_root / "get.ps1").read_text(encoding="utf-8")
    check("get.ps1 finds the unpacked root by install.bat, not by a hardcoded name",
          "install.bat" in _get and "Join-Path $tmp 'omnius'" not in _get)
    check("Node has a direct route too, so a winget-less box is not half-installed",
          "Install-NodeDirect" in _inst0 and "nodejs.org/dist/index.json" in _inst0)
    # The direct routes are for machines nobody here owns, so they need a way to
    # be exercised that is not "boot a sandbox and wait 18 minutes for an MSI".
    check("the direct routes can be tested without a sandbox",
          "[switch]$NoWinget" in _inst0
          and "(Test-Cmd 'winget') -and (-not $NoWinget)" in _inst0)

    # == a direct install must not claim success it did not have ==============
    # 2026-08-15, first real run of these routes: Move-Item failed with "Could
    # not find a part of the path" and the very next line said "[OK] Node
    # v24.19.0 installed". Two causes, both fixed here. The script runs with
    # ErrorActionPreference 'Continue', so a failing cmdlet prints red and
    # CARRIES ON - the try/catch around it was decorative until each installer
    # set 'Stop' for itself.
    for _fn in ("Install-NodeDirect", "Install-FfmpegDirect"):
        _body = _inst0[_inst0.index(f"function {_fn}"):]
        _body = _body[:_body.index("\nfunction ", 10)]
        check(f"{_fn}: failures reach the catch instead of scrolling past",
              "$ErrorActionPreference = 'Stop'" in _body)
        check(f"...and it verifies the binary before reporting success",
              "throw" in _body and _body.index("Test-Path") < _body.rindex("Write-Status 'OK'"))
    # The other half: Move-Item does not create intermediate directories, and
    # Node runs BEFORE the ffmpeg step that used to create the parent - so it
    # failed on every machine where ffmpeg had not been installed first.
    check("a portable tool creates its own parent directory",
          "New-Item -ItemType Directory -Force -Path (Split-Path $dest -Parent)" in _inst0)
    # gyan.dev serves from one host and crawled at 7 MB of 106 in a VM; the
    # identical zip sits behind GitHub's CDN, which is where winget gets it.
    check("ffmpeg comes from the CDN copy, with the direct site as fallback",
          "GyanD/codexffmpeg/releases/latest" in _inst0
          and "gyan.dev/ffmpeg/builds" in _inst0)
    check("...and takes the essentials build, not the 240 MB full one",
          "essentials_build" in _inst0)

    # == never report a failure whose reason you just threw away =============
    # 2026-08-15, fresh Windows 10 VM: "pymupdf FAILED" and "faster-whisper
    # install failed" with NOTHING above them. pip had succeeded - the packages
    # could not LOAD - and the check that decided this ran the import with
    # `2>nul`, discarding the one line that said why.
    check("a failing import prints its error instead of discarding it",
          '"import $module"" 2>&1' in _inst0 and "function Test-PyImport" in _inst0)
    # Scoped to IMPORT probes: a silent `git clone` is fine, a silent import is
    # what made two failures unexplainable. Presence probes go through the same
    # helper and simply ignore the text it returns, so there is one way to ask.
    _hidden = re.findall(r'python -c ""import [^"]*"" 2>nul', _inst0)
    check("...and no import check still sends its stderr to nul",
          not _hidden, f"{_hidden}")
    # The cause, and the one part of it we can actually fix: C++ extensions
    # (pymupdf, ctranslate2 under faster-whisper) need the MSVC runtime, which a
    # bare Windows lacks - Python ships vcruntime140.dll but not
    # vcruntime140_1.dll. Pure-Python packages install fine beside them, which
    # is why the failure looks arbitrary.
    check("a DLL-load failure offers the runtime that fixes it",
          "DLL load failed" in _inst0 and "vc_redist.x64.exe" in _inst0)
    check("...from Microsoft's own permanent link",
          "aka.ms/vs/17/release/vc_redist.x64.exe" in _inst0)
    check("...treating 3010 (reboot advised) as success, not failure",
          "0, 3010" in _inst0)
    check("...and re-testing the import afterwards rather than assuming",
          "still not loading" in _inst0)
    # Same shape of lie, different tool: node_modules existing is not remotion
    # working. Newer npm blocks install scripts, and esbuild fetches its binary
    # in one - so every package can be present and rendering still dies.
    check("remotion checks for esbuild's binary, not just for node_modules",
          "@esbuild" in _inst0 and "esbuild.exe" in _inst0)
    # huggingface prints a token warning and a symlink warning on a normal
    # Windows box. Neither is actionable by someone running an installer, and
    # together they are twelve alarming lines meaning "it worked".
    check("the whisper pre-warm does not print warnings nobody can act on",
          "HF_HUB_DISABLE_SYMLINKS_WARNING" in _inst0)
    # A first install creates .env from the example, so all three Discord values
    # are blank by definition. Printing one "[X] ... is empty" per key, then a
    # verdict, then the setup offer repeating it, made a normal install read as
    # a failure - and the inner marker doubled up as "[!   ]      [X] ...".
    check("a blank Discord config is stated once, not four times",
          "its three values in .env are still blank" in _inst0
          and "$missing.Count -ge 3" in _inst0)
    check("...while a PARTIAL config still lists what is wrong, undoubled",
          "-replace '^\\s*\\[[^\\]]*\\]\\s*', ''" in _inst0)

    # == a SYSTEM shell has no user profile ==================================
    # 2026-08-15, on a real machine: the Claude CLI installer died with
    # `EPERM: mkdir 'C:\WINDOWS\system32\config\systemprofile\.cache'`. That
    # path is LocalSystem's home - so the shell had no user profile, and
    # everything per-user this installs (the sign-in, ~\.claude, the desktop
    # icon, every hook path) would have belonged to an account no human uses.
    check("install refuses to run as SYSTEM, before installing anything",
          "systemprofile" in _inst0 and "NT AUTHORITY\\SYSTEM" in _inst0)
    check("...and says so where a person can act on it, naming the home it found",
          "has no user profile" in _inst0)
    # Elevated-as-a-user is normal and must keep working - Git's own installer
    # asks for exactly that.
    check("...without refusing a normal elevated shell",
          "This is NOT the same thing as \"elevated\"" in _inst0)
    check("a failing Claude install names the cause it can recognise",
          "that path is the SYSTEM account's home" in _inst0.replace("''", "'"))
    check("presence is decided by the FILE, not only by PATH",
          "function Test-Claude" in _inst0 and "claude.exe" in _inst0,
          "Get-Command does not reliably re-scan PATH inside the same window")
    check("a failed install no longer claims it was 'installed but not on PATH'",
          "installed but not on PATH in this window" not in _inst0,
          "it sent him restarting a window that was never the problem")

    # == a third-party installer must not reach into our session ==============
    # FOUND ON A REAL FRESH INSTALL (2026-08-11). `irm … | iex` ran Claude's
    # installer INSIDE our PowerShell session; it succeeded and left StrictMode
    # on. The very next line of ours - `$t.Test` on a hashtable without that
    # key - became a TERMINATING PropertyNotFoundStrict, and the install died
    # right there, after the prerequisites and before everything else.
    # The LAST mention is the call; the first one is the line that announces it.
    _claude_call = _inst0[_inst0.rindex("claude.ai/install.ps1") - 900:
                          _inst0.rindex("claude.ai/install.ps1") + 300]
    check("the Claude installer runs in a CHILD process, not iex into ours",
          "& powershell -NoProfile -ExecutionPolicy Bypass -Command" in _claude_call
          and "try { Invoke-RestMethod https://claude.ai/install.ps1 | Invoke-Expression }" not in _inst0,
          "anything it changes - StrictMode, ErrorActionPreference, cwd - would persist")
    # Its output is captured rather than streamed: that installer signs off with
    # "Installation complete!" and a paragraph about editing PATH by hand, both
    # of which are wrong HERE - our install is three steps in, and we set that
    # PATH entry ourselves. Mid-run it reads as "you are done" (2026-08-14).
    check("...with its output captured, so its 'Installation complete!' cannot "
          "be mistaken for ours",
          "2>&1 |" in _claude_call and "Out-String" in _claude_call)
    check("...and shown in full the moment anything actually fails",
          "its output:" in _inst0)
    # Belt and braces: even if strictness leaks in some other way, optional
    # hashtable keys must be read by INDEX, which returns $null instead of
    # throwing. Only Test/Installer/WingetId are optional; the rest are on
    # every entry and stay as dots.
    for _k in ("Test", "Installer", "WingetId"):
        check(f"optional key {_k} is read by index, not dot",
              f"$t['{_k}']" in _inst0 and f"$t.{_k}" not in _inst0,
              "dot access on a missing key is fatal under StrictMode")

    # == an orphaned .busy stamp must not silence the desk forever ============
    # FIRST MESSAGE ON A FRESH INSTALL, 2026-08-11. He ran `claude` once to
    # authenticate - as GETTING-STARTED tells him to - and closed the window.
    # The UserPromptSubmit hook had stamped .busy; the Stop hook never ran, so
    # it was orphaned. The watchdog then refused to start ANY run ("a turn IS
    # running, it is just slow") and his first Discord message was never
    # answered. The alarm was right that nothing worked and wrong about why.
    print("== orphaned busy stamp ==")
    _turns, _sa2, _ra2 = wd.TURNS, wd.session_alive, wd.run_active
    try:
        _t = SAND / "turnstamps"; _t.mkdir(exist_ok=True)
        wd.TURNS = _t
        (_t / "demo.app.busy").write_text("", encoding="utf-8")
        wd.session_alive = lambda s: False
        wd.run_active = lambda s: False
        check("a stamp with nothing alive behind it is NOT busy",
              wd.turn_busy("demo.app") is False,
              "otherwise a closed terminal silences that desk permanently")
        wd.session_alive = lambda s: True
        check("...but a live terminal claim still is", wd.turn_busy("demo.app") is True)
        wd.session_alive = lambda s: False
        wd.run_active = lambda s: True
        check("...and so is an active run", wd.turn_busy("demo.app") is True)
        (_t / "demo.app.busy").unlink()
        check("no stamp is never busy", wd.turn_busy("demo.app") is False)
        (_t / "demo.app.busy").write_text("", encoding="utf-8")
        def _boom(s):
            raise OSError("cannot read")
        wd.session_alive = _boom
        check("...and if it cannot tell, it assumes busy",
              wd.turn_busy("demo.app") is True,
              "guessing 'free' would put two brains on one desk")
    finally:
        wd.TURNS, wd.session_alive, wd.run_active = _turns, _sa2, _ra2
    _wd_src = (real_root / "tools" / "discord" / "watchdog.py").read_text(encoding="utf-8")
    check("every place that trusted the raw stamp now goes through turn_busy()",
          _wd_src.count('.busy").is_file()') == 1,
          "one read remains, inside turn_busy itself")

    # == a turn that died on an API error ====================================
    # 2026-08-14, tool.transcribe: turn started 09:34, the API answered 529
    # Overloaded at 09:37, and Claude Code does not run its Stop hook on that
    # path. The stamp outlived the turn, the session stayed alive and idle, so
    # turn_busy said yes and EVERY recovery is gated on that bit - no nudge, no
    # recover_bridge, two messages unread. Quiet alone must not release a stamp
    # (lessons.md), but an API error as the last thing written is the turn
    # reporting its own death, and that is evidence.
    print("== a turn that died on an API error ==")
    _t2, _hd, _cf = wd.TURNS, wd.history_dir_for, wd.cwd_for
    try:
        _tt = SAND / "turns2"; _tt.mkdir(exist_ok=True)
        _hist = SAND / "hist-apierror"; _hist.mkdir(exist_ok=True)
        wd.TURNS = _tt
        wd.history_dir_for = lambda cwd: _hist
        wd.cwd_for = lambda s: SAND
        _conv = _hist / "conv.jsonl"

        def _write(last_text, kind="assistant"):
            _conv.write_text("\n".join([
                json.dumps({"type": "user", "message": {"content": "hi"}}),
                json.dumps({"type": kind,
                            "message": {"content": [{"type": "text", "text": last_text}]}}),
            ]) + "\n", encoding="utf-8")

        check("no stamp means nothing to release", wd.turn_died("d.k") is False)
        (_tt / "d.k.busy").write_text("{}", encoding="utf-8")
        check("a stamp with no conversation at all is left alone",
              wd.turn_died("d.k") is False)

        _write("API Error: 529 Overloaded. This is a server-side issue")
        os.utime(_conv, (time.time() - 600, time.time() - 600))
        os.utime(_tt / "d.k.busy", (time.time() - 900, time.time() - 900))
        check("...but an API error as the LAST thing written is the turn's death",
              wd.turn_died("d.k") is True)

        _write("Done, here is the transcript.")
        os.utime(_conv, (time.time() - 600, time.time() - 600))
        os.utime(_tt / "d.k.busy", (time.time() - 900, time.time() - 900))
        check("a turn that ended with real work is NOT dead",
              wd.turn_died("d.k") is False,
              "only an API error counts; quiet alone must never release a stamp")

        _write("API Error: 529 Overloaded")          # fresh: may still be retrying
        check("a JUST-written API error is a retry in flight, not a death",
              wd.turn_died("d.k") is False,
              f"Claude Code retries; wait {wd.API_ERROR_QUIET_SECONDS}s")
        check("...and that grace is short enough to matter",
              wd.API_ERROR_QUIET_SECONDS <= 120)

        # An error from an EARLIER turn must not kill the current one.
        _write("API Error: 529 Overloaded")
        os.utime(_conv, (time.time() - 600, time.time() - 600))
        (_tt / "d.k.busy").write_text("{}", encoding="utf-8")   # stamp is newer
        check("an API error that PREDATES the stamp belongs to an earlier turn",
              wd.turn_died("d.k") is False)

        # And the whole point: turn_busy must act on it, in ONE place.
        _write("API Error: 529 Overloaded")
        os.utime(_conv, (time.time() - 600, time.time() - 600))
        os.utime(_tt / "d.k.busy", (time.time() - 900, time.time() - 900))
        wd.session_alive = lambda s: True           # session alive and idle: the real case
        wd.run_active = lambda s: False
        check("turn_busy releases a stamp whose turn died, even with a live session",
              wd.turn_busy("d.k") is False,
              "this is the bit every recovery path reads")
        check("...and the stamp is actually gone, so recovery can proceed",
              not (_tt / "d.k.busy").is_file())
    finally:
        wd.TURNS, wd.history_dir_for, wd.cwd_for = _t2, _hd, _cf
        wd.session_alive, wd.run_active = _sa2, _ra2
    check("the deaf alarm no longer claims 'no live session' when there IS one",
          "its turn stopped without finishing" in _wd_src,
          "it sent him hunting a crash that had not happened")

    # == "Omnius is typing..." while a desk works =============================
    # His ask, 2026-08-13: 👀 says received, then nothing moves for minutes and
    # a long turn is indistinguishable from a dead one. These pin the two
    # properties that made typing the right primitive: it only appears while
    # work is really happening, and it costs one call per channel per window.
    print("== busy marker (typing) ==")
    _typed = []
    _ra3, _tb3 = wd.run_active, wd.turn_busy
    _real_typing = wd.api.trigger_typing
    try:
        wd.api.trigger_typing = lambda cid: _typed.append(cid)
        _map = {"1": wd.Target("demo.app", "app", "demo"),
                "2": wd.Target("demo.backend", "backend", "demo"),
                "3": wd.Target(None, "general", "demo")}
        wd.run_active = lambda s: s == "demo.app"
        wd.turn_busy = lambda s: False
        wd._typing_sent.clear()
        wd.show_working(_map)
        check("a desk with a live run makes its channel show typing", _typed == ["1"])
        check("...and an idle desk's channel does NOT",
              "2" not in _typed, "a marker that is always on says nothing")
        check("...nor does a channel with no desk behind it", "3" not in _typed)

        _typed.clear()
        wd.show_working(_map)
        check("the next tick does not re-trigger inside the refresh window",
              _typed == [], f"one call per {wd.TYPING_REFRESH_SECONDS}s, not per 3s tick")
        wd._typing_sent["1"] = 0.0          # last triggered long ago
        wd.show_working(_map)
        check("...but it IS refreshed once the window passes", _typed == ["1"],
              "Discord drops the indicator after ~10s")
        check("the refresh window is shorter than Discord's ~10s expiry",
              wd.TYPING_REFRESH_SECONDS < 10,
              "otherwise the indicator blinks out mid-run")

        # A terminal turn is work too - from a phone the question is the same.
        _typed.clear(); wd._typing_sent.clear()
        wd.run_active = lambda s: False
        wd.turn_busy = lambda s: s == "demo.backend"
        wd.show_working(_map)
        check("a terminal mid-turn shows typing too, not just a headless run",
              _typed == ["2"])

        # Cosmetic must never be load-bearing.
        _typed.clear(); wd._typing_sent.clear()
        def _boom_typing(cid):
            raise wd.api.ApiError("403 forbidden")
        wd.api.trigger_typing = _boom_typing
        wd.turn_busy = lambda s: True
        ok = True
        try:
            wd.show_working(_map)
        except Exception:                                        # noqa: BLE001
            ok = False
        check("a typing call that fails does not break the tick", ok,
              "the busy marker is cosmetic; delivery is not")
        wd.api.trigger_typing = lambda cid: _typed.append(cid)
        wd.show_working(_map)
        check("...and the failed channel backs off instead of retrying every tick",
              _typed == [], "a 403 every 3s would flood the log")
    finally:
        wd.run_active, wd.turn_busy = _ra3, _tb3
        wd.api.trigger_typing = _real_typing
        wd._typing_sent.clear()

    # == every desk carries the SAME allow-list ===============================
    # A desk missing an entry stops and asks, and over Discord that is a
    # question on a screen nobody is watching - the exact failure the posture
    # exists to prevent. Kept by hand the list drifted twice over: the
    # 2026-08-12 widening reached only the seven TRACKED settings files, so
    # every project desk (projects\ is gitignored) kept the original eleven;
    # and `Artifact` was in none of them, which is what interrupted a desk
    # mid-answer on 2026-08-13. One definition, one writer, and this check.
    print("== desk allow-lists are in sync ==")
    sys.path.insert(0, str(real_root / "tools" / "discord"))
    import sync_permissions as sp                                  # noqa: E402
    _files = sp.settings_files()
    # Counting the REAL tree measured how many projects this machine happens to
    # have - it wanted >= 8, and a fresh clone has six. The RULE is what
    # matters, so it runs against a tree built for the purpose, including the
    # desk one level deeper inside a project: exactly the shape the 2026-08-12
    # widening walked straight past.
    _psand = SAND / "permsync"
    for _rel in (".claude", "daybook/.claude", "tools/email/.claude",
                 "projects/demo/.claude", "projects/demo/app/.claude"):
        (_psand / _rel).mkdir(parents=True, exist_ok=True)
        (_psand / _rel / "settings.json").write_text('{"permissions":{"allow":[]}}',
                                                     encoding="utf-8")
    _sp_root = sp.ROOT
    try:
        sp.ROOT = _psand
        _found = {p.relative_to(_psand).as_posix() for p in sp.settings_files()}
    finally:
        sp.ROOT = _sp_root
    check("sync_permissions finds every desk: root, daybook, tools, project",
          {".claude/settings.json", "daybook/.claude/settings.json",
           "tools/email/.claude/settings.json",
           "projects/demo/.claude/settings.json"} <= _found,
          f"found {sorted(_found)}")
    check("...including the desk nested inside a project (what the widening missed)",
          "projects/demo/app/.claude/settings.json" in _found)
    check("...and it still finds this instance's own desks", len(_files) >= 4,
          f"found {len(_files)}")
    _shortfall = {}
    for _p in _files:
        try:
            _d = json.loads(_p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        _miss = [t for t in sp.ALLOW if t not in (_d.get("permissions", {}).get("allow") or [])]
        if _miss:
            _shortfall[_p.name if _p.parent.parent == real_root else
                       str(_p.relative_to(real_root))] = _miss
    check("every desk carries the full shared allow-list",
          not _shortfall, f"short: {list(_shortfall)[:3]}")
    check("Artifact is allowed - a desk must not need permission to publish its answer",
          "Artifact" in sp.ALLOW)
    # The fence that must survive every widening.
    _denyless = []
    for _p in _files:
        try:
            _d = json.loads(_p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not any(".env" in r for r in (_d.get("permissions", {}).get("deny") or [])):
            _denyless.append(str(_p.relative_to(real_root)))
    check("...and every one still refuses to read .env (the one fence he kept)",
          not _denyless, f"no .env deny in: {_denyless}")
    # A tool that draws a MENU in the desk's terminal can never be answered:
    # nobody is in front of that terminal, and the bus can only type while no
    # turn is running - so the widget blocks the very turn that would have to
    # end for anyone to reach it. 2026-08-13: a project desk asked which
    # browser to drive and sat there; "ok" in Discord only allowed it to ASK.
    check("AskUserQuestion is DENIED, not merely left off the allow-list",
          "AskUserQuestion" in sp.DENY and "AskUserQuestion" not in sp.ALLOW,
          "off-the-list prompts, he says ok, and it hangs exactly the same way")
    _bothways, _undenied = [], []
    for _p in _files:
        try:
            _d = json.loads(_p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        _perm = _d.get("permissions", {})
        for _t in sp.DENY:
            if _t in (_perm.get("allow") or []):
                _bothways.append(str(_p.relative_to(real_root)))
            if _t not in (_perm.get("deny") or []):
                _undenied.append(str(_p.relative_to(real_root)))
    check("...on every desk", not _undenied, f"still allowed to hang: {_undenied[:3]}")
    check("...and no desk both allows and denies it",
          not _bothways, f"contradictory: {_bothways[:3]}")

    # A desk that ends its turn saying "te aviso cuando esté" has stopped
    # existing, and nothing is building (2026-08-13, a project desk, after an hour
    # of real work). It is worse than a crash because it READS like progress.
    _sk6 = (real_root / ".claude" / "skills" / "omnius" / "SKILL.md").read_text(encoding="utf-8")
    check("the skill forbids ending a turn with a promise to continue",
          "Never end a turn promising to continue" in _sk6)
    check("...and says plainly that a finished turn has no background",
          "you stop existing" in _sk6,
          "there is no timer, no 'later', nothing keeps running")
    check("...and offers the real mechanism instead of the promise",
          "schedule.py add --in" in _sk6)
    check("...capped, so a desk cannot re-queue itself forever",
          "--loop auto" in _sk6 and "never self-extend" in _sk6.lower(),
          "the budgeted loop replaced the two-continuation prose cap (D5)")
    # The command it tells desks to run must actually parse.
    _sched = (real_root / "tools" / "discord" / "schedule.py").read_text(encoding="utf-8")
    check("schedule.py really has the --in flag the skill hands out",
          '"--in"' in _sched and 'dest="in_"' in _sched,
          "a skill instruction that does not run is worse than none")
    check("...and enforces the loop on self-addressed adds, not prose",
          "must carry --loop" in _sched)
    check("...and --in resolves to an absolute time when written",
          "must say\n            # WHEN" in _sched or "must say" in _sched,
          "a restored backup would otherwise re-fire it relative to the restore")

    # Denying the widget is only half of it: the browser case DEMANDS a choice
    # (the Chrome extension refuses to act with two connected), and the only
    # other way to answer is a click in Chrome. His objection, and it is the
    # right one: "how am i gonna accept if i am on mobile on discord?" So the
    # choice has to be a SETTING, decided once at the desk.
    sys.path.insert(0, str(real_root / "tools"))
    import omnius_config as _oc                                  # noqa: E402
    check("there is a setting for which browser a desk drives",
          hasattr(_oc, "browser_device_id"))
    check("...and it defaults to empty, which means ask (correct with one browser)",
          _oc.browser_device_id({}) == "" or isinstance(_oc.browser_device_id(), str))
    check("...and the example config documents it, so a fresh install can find it",
          "[browser]" in (real_root / "config" / "omnius.example.ini")
          .read_text(encoding="utf-8-sig"))
    _skill = (real_root / ".claude" / "skills" / "omnius" / "SKILL.md").read_text(encoding="utf-8")
    check("the skill tells a desk to READ that setting instead of asking",
          "browser_device_id()" in _skill and "select_browser" in _skill)
    check("...and forbids AskUserQuestion in words, not just in settings.json",
          "Never call `AskUserQuestion`" in _skill,
          "a desk reads the skill; it never reads its own allow-list")
    check("...and forbids asking him to click anything on the PC",
          "Never ask him to click something on the PC" in _skill,
          "he may be at home; a click he cannot reach is a dead desk")

    # An "ok" must TEACH the list, or the same tool asks again tomorrow.
    _real_learned = sp.LEARNED
    try:
        sp.LEARNED = SAND / "allow-learned.json"
        check("a fresh instance has learned nothing, and says so as an empty list",
              sp.learned() == [])
        check("an approval is remembered", sp.learn("mcp__brand-new") is True)
        check("...and is not remembered twice", sp.learn("mcp__brand-new") is False)
        check("...and something already in the shared list is not duplicated",
              sp.learn("Artifact") is False)
        check("effective_allow is the shared list PLUS what he approved",
              "mcp__brand-new" in sp.effective_allow()
              and len(sp.effective_allow()) == len(sp.ALLOW) + 1)
        # It feeds an allow-list, so it must not accept a path or a command.
        check("a tool name with a path in it is refused",
              sp.learn("Bash(rm -rf /)") is False,
              "an approval buys the TOOL, never a particular argument")
        check("...and so is an empty or absent one", sp.learn("") is False
              and sp.learn(None) is False)
        sp.LEARNED.write_text("{ not json", encoding="utf-8")
        check("a mangled learned-file means nothing learned, never allow-everything",
              sp.learned() == [])
        sp.LEARNED.write_text('"allow-all"', encoding="utf-8")
        check("...and neither does a file that is not a list", sp.learned() == [])
    finally:
        sp.LEARNED = _real_learned
    _rem = _wd_src[_wd_src.index("def remember_allowed"):][:1600]
    check("only an ALLOW teaches the list - a refusal teaches nothing",
          'if behavior != "allow":' in _rem)
    check("...and learning can never swallow the answer he is waiting on",
          "except Exception" in _rem and "return \"\"" in _rem)
    check("config\\allow-learned.json is gitignored - it names THIS install's servers",
          re.search(r"^config/\*\s*$",
                    (real_root / ".gitignore").read_text(encoding="utf-8"), re.M) is not None,
          "a release must not ship the names of his integrations")
    # An example file is copied verbatim into every release, so a placeholder
    # shaped like real data fails the release audit - which refuses rather than
    # filters, by design. guests.example.ini shipped 18 zeros as a sample id on
    # 2026-08-12 and made every -Fresh build refuse; nobody noticed, because
    # nobody built a release between then and 2026-08-13.
    for _ex in sorted((real_root / "config").glob("*.example.ini")):
        check(f"{_ex.name} has no id-shaped placeholder (it would refuse the release)",
              not re.search(r"=\s*\d{17,20}\b", _ex.read_text(encoding="utf-8")),
              "use PASTE-... rather than digits")
    check("...and the shared list names no server of his either",
          not [t for t in sp.ALLOW if t.startswith("mcp__")
               and t not in ("mcp__claude-in-chrome", "mcp__computer-use",
                             "mcp__Claude_Browser", "mcp__visualize",
                             "mcp__ccd_session", "mcp__ccd_session_mgmt",
                             "mcp__mcp-registry", "mcp__scheduled-tasks")],
          "an integration he connected belongs in the learned file, not the repo")

    check("autostart repairs the allow-list the same way it repairs hook paths",
          "sync_permissions.py" in (real_root / "tools" / "discord" / "autostart.ps1")
          .read_text(encoding="utf-8"),
          "a fresh install must not come up short")

    # == !reload must not shed its supervisor =================================
    # service_runner uses runpy, so it IS the watchdog process. execv'ing a bare
    # `python watchdog.py` therefore replaced the supervisor instead of the
    # child: the task read Ready, its 60s self-heal fired into a no-op, and the
    # redirected log stopped. Source-level, because exercising it means
    # replacing this test's own process image.
    print("== !reload keeps its supervisor ==")
    _sr = (real_root / "tools" / "service_runner.py").read_text(encoding="utf-8")
    check("service_runner tells the target how to re-exec through it",
          'OMNIUS_SERVICE_RUNNER"] = ' in _sr)
    # The body moved into do_reload when !update learned to share it (D-phase,
    # 2026-08-15); the invariants pinned here are unchanged.
    _reload_src = _wd_src[_wd_src.index("def do_reload"):][:2400]
    check("!reload re-execs through service_runner when it is there",
          "OMNIUS_SERVICE_RUNNER" in _reload_src,
          "otherwise every reload silently unsupervises the bus")
    check("...and still works when started by hand, with no supervisor",
          "if runner and Path(runner).is_file()" in _reload_src)
    check("!reload still refuses code that would not start",
          "reload **REFUSED**" in _reload_src,
          "the watchdog is the only thing listening; a bad re-exec is unreachable")

    # == stop-omnius: the counterpart to starting it ==========================
    # He could not delete a workspace because the services kept coming back:
    # the tasks self-heal every 60s by design, so "stop the process" is not
    # stopping Omnius (2026-08-11).
    print("== stop-omnius ==")
    _stop = (real_root / "stop-omnius.ps1").read_text(encoding="utf-8")
    check("stop-omnius exists, with a .bat shim like the other entry points",
          (real_root / "stop-omnius.bat").is_file())
    check("it DISABLES the tasks, not just stops them",
          "Disable-ScheduledTask" in _stop,
          "a task whose trigger fires every minute restarts before you finish reading")
    check("it matches processes by COMMAND LINE, not image name",
          "$_.CommandLine -like $rootPat" in _stop and "Name -eq 'python" not in _stop,
          "killing every python.exe would be a spectacular way to stop Omnius")
    check("it leaves Claude desks alone unless -All is given",
          "-not $All" not in _stop and "Use -All to close them too" in _stop,
          "a desk mid-turn is work; losing it silently is worse than leaving it")
    check("it reports SURVIVORS instead of claiming success",
          "still running:" in _stop)
    _lau2 = (real_root / "tools" / "omnius_launcher.ps1").read_text(encoding="utf-8")
    check("...and the launcher re-enables a disabled task, so stopping is reversible",
          "Enable-ScheduledTask" in _lau2,
          "otherwise the icon silently does nothing after a stop")

    # == the server structure must SELF-HEAL ==================================
    # ensure_structure ran once, at watchdog startup. Delete a channel on a
    # running instance and it stayed gone until someone restarted the watchdog
    # or ran a CLI verb - which is not something an owner should have to know
    # (2026-08-11: he cleared the server to start clean and was left with a
    # bot that recognised nothing).
    print("== structure self-heal ==")
    _wsrc = (real_root / "tools" / "discord" / "watchdog.py").read_text(encoding="utf-8")
    _refresh = _wsrc.split("MAP_REFRESH_SECONDS:")[1][:900]
    check("the periodic refresh re-stamps the structure, not just the map",
          "ensure_structure" in _refresh,
          "find-or-create is free when nothing is missing")
    check("...and a failure there cannot stop the bus",
          "structure re-check failed" in _wsrc)
    _i2 = (real_root / "install.ps1").read_text(encoding="utf-8")
    check("install creates the channels itself, not only via the watchdog",
          "api.py') ensure" in _i2 or 'api.py") ensure' in _i2,
          "'installed, now run a command' is not installed")
    check("...and re-checks Discord after guided setup, so it knows it can",
          _i2.count("api.py') config-check") >= 2)
    # Every api.py verb install invokes must actually exist: a typo here fails
    # silently and skips the step it guards. `check` is not a verb.
    import subprocess as _sp
    _verbs = _sp.run([sys.executable, str(real_root / "tools" / "discord" / "api.py"), "--help"],
                     capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    import re as _re
    _used = set(_re.findall(r"api\.py'\)\s+([a-z][a-z-]*)", _i2))
    _unknown = sorted(v for v in _used if v not in _verbs)
    check("...and every api.py verb install calls really exists",
          not _unknown, f"unknown verb(s): {_unknown}")

    # == the code must run on the Python we PROMISE ===========================
    # README, install.ps1 and GETTING-STARTED all say 3.10+. watchdog.py used
    # PEP 701 (same quote reused inside an f-string expression), which is 3.12+
    # only, so on a fresh install with Python 3.11 the file would not PARSE and
    # the watchdog crash-looped every 60s. It never failed here because this
    # machine runs 3.14 - the promise and the code had quietly disagreed.
    # ast.parse(feature_version=(3,10)) does NOT catch it: PEP 701 changed the
    # tokenizer, so a modern interpreter simply accepts it.
    print("== python compatibility ==")
    _compat = subprocess.run([sys.executable, str(real_root / "tools" / "check_py_compat.py")],
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
    check("no source file needs a newer Python than we promise (3.10+)",
          _compat.returncode == 0, _compat.stdout.strip()[-400:])
    # And the checker itself must still bite - a scanner that always passes is
    # worse than none. Its first version compared only against the INNERMOST
    # open f-string and declared the broken file clean.
    sys.path.insert(0, str(real_root / "tools"))
    import check_py_compat as _cpc                                # noqa: E402
    _BROKEN = '''x = f"{f' (pid {", ".join(p)})' if p else ''}. "\n'''
    # WHICH bite depends on the interpreter running the suite, and that is not a
    # detail: `python` here is 3.11 (the fleet's own), while a conda shell on the
    # same machine is 3.14. The token scan needs FSTRING_START, which is 3.12+;
    # below that the syntax is simply a SyntaxError, so compile() is the bite.
    # Asserting the 3.12 shape on 3.11 failed the suite for a checker that was
    # working correctly - a red test nobody can fix teaches people to ignore red.
    if _cpc.CAN_TOKENIZE_PEP701:
        check("...and the checker detects the exact shape that broke it",
              _cpc.nested_fstring_quotes(_BROKEN) != [],
              "the clash is with the OUTER quote, two levels up")
        check("...without flagging ordinary nested quotes that are legal on 3.10",
              _cpc.nested_fstring_quotes("""x = f"{', '.join(p)}"\n""") == []
              and _cpc.nested_fstring_quotes('''y = f'{", ".join(p)}'\n''') == [])
    else:
        check("...and on this interpreter the parser itself is the check",
              _cpc.parse_error(_BROKEN, "<broken>") is not None,
              "PEP 701 syntax must not parse below 3.12")
        check("...without flagging ordinary nested quotes that are legal on 3.10",
              _cpc.parse_error("""x = f"{', '.join(p)}"\n""", "<ok>") is None
              and _cpc.parse_error('''y = f'{", ".join(p)}'\n''', "<ok>") is None)

    # THREE failures from the first downloaded install on another PC, 2026-08-11.
    _as = (real_root / "tools" / "discord" / "autostart.ps1").read_text(encoding="utf-8")
    check("scheduled tasks use the REAL current identity, not composed env vars",
          "Get-TaskUserId" in _as
          and r'-UserId "$env:USERDOMAIN\$env:USERNAME"' not in _as,
          "composing it gave an account Windows could not map: HRESULT 0x80070534")
    check("...falling back to the SID, since name->SID is the step that failed",
          "$me.User.Value" in _as)
    check("install strips quotes people paste around paths",
          """.Trim().Trim('"').Trim("'").Trim()""" in _inst0,
          'Explorer copy-as-path adds them; a quoted D: became a drive named \'"D\'')
    check("a failed mkdir cannot report success",
          "-ErrorAction Stop" in _inst0 and "-PathType Container" in _inst0,
          "non-terminating error + Continue = the catch never fires and OK prints anyway")
    check("a backup folder that is SET but unusable is re-offered, not trusted",
          "set but unusable" in _inst0)
    check("install unblocks downloaded scripts (mark-of-the-web)",
          "Unblock-File" in _inst0,
          "unzipped .ps1 files are refused under the default RemoteSigned policy")
    _lau = (real_root / "tools" / "omnius_launcher.ps1").read_text(encoding="utf-8")
    check("the desktop icon starts services even when no task is registered",
          "service_runner.py" in _lau and "Start-Process -FilePath $pyw" in _lau,
          "it used to `continue` past a missing task and start nothing at all")
    check("...without stacking a duplicate on a second double-click",
          "Win32_Process" in _lau and "if ($running) { continue }" in _lau)

    # == one duplicate heading must not cost every setting ====================
    # install prepended a "[backup]" block to a template that ALREADY had one.
    # configparser raises DuplicateSectionError, load() fell back to defaults,
    # and a file that was 99% right behaved as if absent: minutes after install
    # printed "backups -> D:\Backups\omnius" the desk correctly reported no
    # backup folder configured (2026-08-11). Language and machine name went too.
    print("== duplicate ini sections ==")
    _cd2 = _oc.CONFIG_DIR
    try:
        _dd = SAND / "dupini"; _dd.mkdir(exist_ok=True)
        _oc.CONFIG_DIR = _dd
        _ex = (real_root / "config" / "omnius.example.ini").read_text(encoding="utf-8")
        _dup = "[backup]" + chr(10) + "folder = X:/bk" + chr(10) * 2 + _ex
        (_dd / "omnius.ini").write_text(_dup, encoding="utf-8")
        _cfg = _oc.load("omnius")
        check("a duplicated section is read leniently, not thrown away",
              (_cfg.get("backup") or {}).get("folder") == "X:/bk",
              "strict parsing lost every setting in the file")
        check("...and the other sections survive too",
              "omnius" in _cfg, "it is the whole file that used to be lost")
        check("...and it is REPORTED, so it gets fixed rather than lived with",
              any("omnius.ini" in x and "leniently" in x for x in _oc.problems()))
    finally:
        _oc.CONFIG_DIR = _cd2
    check("install writes ini keys via a helper that cannot duplicate a section",
          "function Set-IniValue" in _inst0
          and "Set-IniValue $iniPath 'backup' 'folder'" in _inst0
          and '"[backup]`r`nfolder =' not in _inst0,
          "prepending a section blindly is what caused it")

    # == who the instance is for ==============================================
    # Facts only. shared\USER.md warns a wrong entry is worse than a missing
    # one, so install asks the two things a person can simply state and never
    # for working style, which must be learned.
    check("install asks the owner's name and language",
          "your name (blank to skip)" in _inst0 and "language you want Omnius" in _inst0)
    check("...defaulting the language to the OS one, so it is one keypress",
          "Get-Culture" in _inst0)
    check("...records it in USER.md, marked as told rather than inferred",
          "answered at install, not inferred" in _inst0)
    check("...and as a config value too, since Omnius writes text of its own",
          any(s == "omnius" and k == "language" for _n, s, k, _e, _d, _t in _oc.SPEC))
    check("...and skipping is allowed (this one is optional, unlike backups)",
          "no name or language given" in _inst0)

    # == backup folder: the one unset setting that is a FAULT =================
    # Every instance backs itself up (2026-08-10). The repo is the system; a
    # machine's notes, projects and media exist on its own disk only, so
    # "nowhere to back up to" is not a default, it is a bet. It nags until set.
    print("== backup folder ==")
    import omnius_config as _ocfg                                 # noqa: E402
    _real_load, _real_get = _ocfg.load, _ocfg.get
    try:
        _ocfg.get = lambda cfg, s, k, e=None, d="", env=None: ""   # unset
        p_, s_ = _ocfg.backup_folder()
        check("an unset backup folder reports NOT SET", p_ is None and "NOT SET" in s_)
        check("...and is raised as a PROBLEM, not left silent",
              any("BACKUP FOLDER" in x.upper() for x in _ocfg.problems()),
              "!config surfaces problems(), so this is visible from Discord")
        _ocfg.get = lambda cfg, s, k, e=None, d="", env=None: str(SAND / "no-such-backup-dir")
        p_, s_ = _ocfg.backup_folder()
        check("a folder that does not exist is reported, not assumed good",
              p_ is not None and "does not exist" in s_)
        _bdir = SAND / "backups"; _bdir.mkdir(exist_ok=True)
        _ocfg.get = lambda cfg, s, k, e=None, d="", env=None: str(_bdir)
        p_, s_ = _ocfg.backup_folder()
        check("a real folder reports ready", s_.startswith("ready:"))
        check("...and the warning stops once it is set",
              not any("BACKUP FOLDER" in x.upper() for x in _ocfg.problems()))
    finally:
        _ocfg.load, _ocfg.get = _real_load, _real_get

    check("the setting is in SPEC, so !config lists it with its source",
          any(s == "backup" and k == "folder" for _n, s, k, _e, _d, _t in _ocfg.SPEC))
    _hb = (real_root / "tools" / "discord" / "watchdog.py").read_text(encoding="utf-8")
    check("the heartbeat nags while it is unset",
          "NO BACKUP FOLDER SET" in _hb)
    check("...but only ONCE A DAY, not every heartbeat",
          "lastBackupNag" in _hb,
          "a line repeated every 30 min is the landfill lesson from 2026-08-08")
    _inst = (real_root / "install.ps1").read_text(encoding="utf-8")
    check("install asks for it, offering a detected folder as the default",
          "backup folder (blank to skip)" in _inst and "OneDrive*" in _inst)
    check("...and says it will keep reminding you if you skip",
          "keep reminding you" in _inst)
    # Asserted on the CODE, not on the memory topic that describes it: that
    # topic is one instance's biography and never ships, so reading it here
    # made this check pass on exactly one machine (2026-08-14).
    check("the backup procedure READS the setting instead of guessing OneDrive",
          "def backup_folder" in (real_root / "tools" / "omnius_config.py")
          .read_text(encoding="utf-8")
          and "ocfg.backup_folder()" in _wsrc)

    # == the fresh-install memory seed ========================================
    # templates\fresh\memory\ IS the memory a new instance boots with, and
    # NOTHING checked it. Measured 2026-08-07, ten days before a colleague was
    # due to install: it knew nothing about routines, the transcribe desk,
    # email or playwright, still said "five, not four" control commands while
    # ten existed, and described a permission model that had been removed.
    # A fresh agent would have been confidently wrong about half the system,
    # which is worse than knowing nothing. These tests exist so the seed cannot
    # rot silently again - status.md item 6: enforce, don't remember.
    print("== fresh-install seed ==")
    SEED = real_root / "templates" / "fresh" / "memory"
    check("the fresh memory seed exists (pack.ps1 -Fresh refuses without it)", SEED.is_dir())

    # memory\ is this instance's biography. install.ps1 and pack.ps1 -Fresh both
    # say so; git did not, and on 2026-08-12 a SECOND install on the same remote
    # pushed its memory over the first machine's - status.md became the other
    # fleet and USER.md was rewritten from the seed. Last writer wins, silently.
    # Pin both halves of the rule: memory\ ignored, the SEED still tracked.
    _gi = (real_root / ".gitignore")
    _gitext = _gi.read_text(encoding="utf-8") if _gi.is_file() else ""
    check("memory\\ is gitignored - it is this instance's biography, not the product",
          re.search(r"^/?memory/\s*$", _gitext, re.M) is not None,
          "two installs on one remote overwrite each other's memory")
    check("...but templates\\fresh\\memory\\ is NOT - a fresh install needs its seed",
          re.search(r"^!templates/fresh/memory/\s*$", _gitext, re.M) is not None)
    if SEED.is_dir():
        seed_text = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                              for p in sorted(SEED.rglob("*.md")))
        missing = [c for c in wd.CONTROL_COMMANDS if c not in seed_text]
        check("the seed documents EVERY control command",
              not missing, f"missing from the seed: {missing}")
        # A tool nobody is told about may as well not ship.
        tool_missing = []
        for d in sorted((real_root / "tools").iterdir()):
            if d.is_dir() and (d / "README.md").is_file() and d.name != "discord":
                if d.name not in seed_text:
                    tool_missing.append(d.name)
        check("the seed introduces every shipped tool",
              not tool_missing, f"never mentioned: {tool_missing}")
        for p_ in sorted(SEED.rglob("*.md")):
            size = len(p_.read_text(encoding="utf-8"))
            rel = p_.relative_to(SEED).as_posix()
            cap = TOPIC_BUDGET if "topics/" in rel else 9_000
            check(f"seed budget: {rel} <= {cap:,} ({size:,})", size <= cap,
                  "the seed is read by every fresh session too")
        # The release guard refuses on these; catching it here is cheaper than
        # a failed -Fresh build, and far cheaper than shipping it.
        # Structural pattern here; the NAME patterns come from the gitignored
        # config\audit-sentinels.txt, same as pack.ps1 and release_sanitize -
        # this file ships, so it must not carry the names itself (2026-08-14).
        ident = {"discord/guild id": r"\b\d{17,20}\b"}
        _sf = real_root / "config" / "audit-sentinels.txt"
        if _sf.is_file():
            for _i, _ln in enumerate(_sf.read_text(encoding="utf-8-sig").splitlines(), 1):
                _ln = _ln.strip()
                if _ln and not _ln.startswith("#"):
                    ident[f"sentinel:{_i}"] = _ln.split("=>")[0].strip()
        else:
            check("no audit-sentinels.txt - fresh instance, seed audited for ids only", True)
        leaks = []
        for p_ in sorted(SEED.rglob("*")):
            if not p_.is_file():
                continue
            body = p_.read_text(encoding="utf-8", errors="replace")
            for label, pat in ident.items():
                if re.search(pat, body, re.I):
                    leaks.append(f"{p_.relative_to(SEED).as_posix()}: {label}")
        check("the seed carries NOTHING identifying (release guard would pass)",
              not leaks, f"{leaks}")
        # The two facts a fresh desk is most dangerous without.
        check("the seed tells a fresh desk that IT is the brake",
              "no permission prompts" in seed_text.lower())
        check("...and that Discord renders no markdown tables",
              "no markdown tables" in seed_text.lower()
              or "renders NO markdown tables" in seed_text)

    always = 0
    for rel in ("orchestrator/MEMORY.md", "orchestrator/status.md", "shared/MEMORY.md", "shared/USER.md"):
        p_ = MEM / rel
        if p_.is_file():
            always += len(p_.read_text(encoding="utf-8"))
    check(f"memory budget: the ALWAYS-READ path <= {ALWAYS_READ_BUDGET:,} chars (is {always:,})",
          always <= ALWAYS_READ_BUDGET,
          "this is paid by every session before it does any work")

    print("== check-in only (the watch mode is deleted) ==")
    # 2026-08-01: session-side watchers died at every turn boundary - three
    # times in one evening - and every death either left the desk deaf or
    # invited a duplicate brain. The fix is not a better watcher; it is NO
    # watcher. These pin the deletion so it cannot creep back.
    iw_src = (real_root / "tools" / "discord" / "inbox_watch.py").read_text(encoding="utf-8")
    iw_code = "\n".join(l for l in iw_src.splitlines() if not l.lstrip().startswith("#"))
    check("inbox_watch has NO watch loop (sessions cannot host daemons)",
          "while True" not in iw_code)
    check("the claim carries no watcherPid (pid liveness is the whole signal)",
          '"watcherPid"' not in iw_code)
    check("check-in stamps turn_started when envelopes wait (feeds the silent-finish announcer)",
          "turn_started" in iw_code)
    # A CALL, not the bare string: the docstring names os.kill precisely to warn
    # against it, and a check that forbids the warning punishes documentation
    # (third time this trap has been walked into in this file).
    check("os.kill is never CALLED for liveness (Windows kills the target)",
          "OpenProcess" in iw_code
          and "os.kill(" not in iw_code.replace("os.kill(pid, 0)", ""))

    # The mirror of the watchdog's busy-stamp guard, caught live 2026-08-01:
    # a ping started a run while the terminal was idle, then a person typed
    # /omnius there and the check-in printed the SAME envelope. Both directions
    # or neither.
    check("check-in REFUSES when a live run already owns the desk",
          "refusing: a headless run" in iw_src and "return 4" in iw_src)
    check("...and refuses BEFORE draining or claiming (the drain is the damage)",
          iw_src.index("active_run(sid") < iw_src.index("claim_path.write_text"))
    check("...comparing the claude session pid, so a run never refuses itself",
          "my_session_pid" in iw_src and "resolve_session_pid(pid_override)" in iw_src)

    # Exercised for real: same sandbox, both branches.
    import importlib as _il
    _iw = _il.import_module("inbox_watch")
    _iw.RUNS = wd.RUNS
    wd.RUNS.mkdir(parents=True, exist_ok=True)
    (wd.RUNS / "guard.json").write_text(
        json.dumps({"session": "guard", "pid": _os0.getpid()}), encoding="utf-8")
    check("a live foreign run is reported as owning the desk",
          _iw.active_run("guard", 99999997) is not None)
    check("the run's OWN check-in is not blocked by its own lease",
          _iw.active_run("guard", _os0.getpid()) is None)
    (wd.RUNS / "guard.json").write_text(
        json.dumps({"session": "guard", "pid": 99999996}), encoding="utf-8")
    check("a dead lease blocks nobody", _iw.active_run("guard", 12345) is None)
    check("no lease at all blocks nobody", _iw.active_run("nosuchdesk", 12345) is None)

    # The refusal's mirror: the ACTIVE RUN is told, in so many words, that it
    # is the active run. 2026-08-01, second live run: it resumed the shared
    # conversation, inherited the terminal's "a run is handling it, I stand
    # down" reasoning, and STOOD DOWN FROM ITSELF - the ping went unanswered
    # while its own worker waited for itself. Identity is stated, not inferred.
    check("the check-in TELLS an active run that it IS the active run",
          "YOU ARE THE ACTIVE HEADLESS RUN" in iw_src
          and "YOURS to answer" in iw_src)
    _sk_iw = (real_root / ".claude" / "skills" / "omnius" / "SKILL.md").read_text(encoding="utf-8")
    check("/omnius teaches the identity rule (believe the check-in, not the transcript)",
          "YOU ARE THE ACTIVE HEADLESS RUN" in _sk_iw)
    check("identity is proven by the run token, not only inferred from pids",
          "OMNIUS_RUN_ID" in iw_src)
    check("/omnius points fresh runs at the bus transcript for conversation context",
          "state\\transcripts" in _sk_iw)
    (wd.RUNS / "guard.json").unlink()

    # (The deaf-desk healing suite lived here until 2026-08-01. The run model
    # deleted the disease: nothing session-side has to stay armed, so nothing
    # can go deaf. Its replacement is "one run per desk" above.)
    import os as _os, time as _t2
    from datetime import datetime as _dt2, timedelta as _td2, timezone as _tz2

    # == the Stop hook: mechanical clears ======================================
    # A turn that ENDED proves two things - the desk is not mid-turn, and no
    # dialog is pending. The hook turns both into state nothing has to remember
    # to update. The sharpest tests are still the quiet ones: nothing printed,
    # exit 0 whatever it is fed.
    print("== Stop hook (turn_end_hook) ==")
    import io as _io, contextlib as _ctx
    import turn_end_hook as teh

    teh.ROOT, teh.PERMS, teh.TURNS = SAND, wd.PERMS, wd.TURNS

    check("cwd -> desk id: the workspace root is the orchestrator",
          teh.session_id_for(str(SAND)) == "orchestrator")
    check("cwd -> desk id: a component folder",
          teh.session_id_for(str(SAND / "projects" / "demo-app" / "app")) == "demo-app.app")
    check("cwd -> desk id: a tool folder",
          teh.session_id_for(str(SAND / "tools" / "discord")) == "tool.discord")
    check("cwd -> desk id: the daybook", teh.session_id_for(str(SAND / "daybook")) == "daybook")
    check("cwd -> desk id: projects\\ itself is NOT a desk",
          teh.session_id_for(str(SAND / "projects" / "demo-app")) is None)
    check("cwd -> desk id: outside the workspace is not a desk",
          teh.session_id_for(str(SAND.parent)) is None)

    def _run_hook(payload):
        """Call the hook exactly as Claude Code does: JSON on stdin, watch stdout."""
        out = _io.StringIO()
        old_stdin = sys.stdin
        sys.stdin = _io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))
        try:
            with _ctx.redirect_stdout(out):
                rc = teh.main()
        finally:
            sys.stdin = old_stdin
        return rc, out.getvalue()

    _app_cwd = str(SAND / "projects" / "demo-app" / "app")
    wd.TURNS.mkdir(parents=True, exist_ok=True)
    wd.PERMS.mkdir(parents=True, exist_ok=True)
    try:
        # The busy stamp (UserPromptSubmit hook) comes down when the turn ends -
        # otherwise the desk would be unreachable from Discord forever.
        _busy = wd.TURNS / "demo-app.app.busy"
        _busy.write_text("{}", encoding="utf-8")
        rc, out = _run_hook({"cwd": _app_cwd})
        check("turn ended: the busy stamp comes down", rc == 0 and not _busy.is_file())
        check("the hook prints NOTHING on stdout", out == "")

        # The false alarm the owner actually saw on 2026-08-01: a .stalled
        # marker outliving the dialog it described. A turn that ended is proof
        # the dialog is gone - a blocked session never reaches the Stop hook.
        _stall = wd.PERMS / "demo-app.app.stalled"
        _stall.write_text(json.dumps({"session": "demo-app.app", "tool": "Bash"}),
                          encoding="utf-8")
        rc, out = _run_hook({"cwd": _app_cwd})
        check("turn ended: the STALLED alarm is taken down", rc == 0 and not _stall.is_file())

        # It must never be able to wedge a session.
        rc, out = _run_hook("{ not json at all")
        check("garbage on stdin: still exits 0, still silent", rc == 0 and out == "")
        rc, out = _run_hook({})
        check("an event with no cwd does not crash the hook", rc == 0)

        # THE point of the file: it must not launch anything. A watcher started
        # here would keep the heartbeat fresh while nothing could wake the
        # session - masking deafness and disabling heal_deaf_desks entirely.
        teh_src = (real_root / "tools" / "discord" / "turn_end_hook.py").read_text(encoding="utf-8")
        teh_code = "\n".join(l for l in teh_src.splitlines() if not l.lstrip().startswith("#"))
        # Narrowed 2026-08-01 when the hook gained `git status`. The original
        # ban on the whole subprocess module was a proxy for the real rule, and
        # the real rule is about a LONG-LIVED process: a watcher launched here
        # could not wake the session but would keep the heartbeat fresh, masking
        # deafness. A synchronous, timed-out `git status` is not that. So forbid
        # what actually hurts - detached launches and re-arming the watcher.
        check("the hook never launches a detached process (that would mask deafness)",
              "Popen" not in teh_code and "os.system" not in teh_code
              and "DETACHED" not in teh_code)
        # "inbox_watch.py", not "inbox_watch": the docstring names the watcher
        # precisely to explain why re-arming here is forbidden, and you can only
        # LAUNCH it by filename. Third time this trap has been walked into.
        check("the hook never re-arms the watcher itself",
              "inbox_watch.py" not in teh_code)
        check("every process it does run is synchronous and time-boxed",
              teh_code.count("subprocess.run(") == teh_code.count("timeout="))
        # It probes nothing any more - there is nothing armed whose liveness
        # could matter. Its whole job is deleting stamps and announcing silence.
        check("the hook probes no pids at all (nothing armed is left to check)",
              "OpenProcess" not in teh_code and "os.kill(" not in teh_code)

        # --- a desk must not finish in silence --------------------------------
        # 2026-08-01: a session was asked for a PDF, built it correctly, and
        # stopped. The file sat on disk while the owner - who could only see
        # Discord - spent the afternoon asking if anything was happening. The
        # work was fine; the silence was the bug.
        print("== finished work announces itself ==")
        teh.TURN_STARTED, teh.OUTBOX = SAND / "watchdog" / "turn_started", wd.OUTBOX
        _tstamp = teh.TURN_STARTED / "demo-app.app.json"
        _tbox = wd.OUTBOX / "demo-app.app"
        teh.TURN_STARTED.mkdir(parents=True, exist_ok=True)
        _tbox.mkdir(parents=True, exist_ok=True)
        _real_changed = teh.changed_files
        teh.changed_files = lambda cwd, since=None: ["slides.pdf", "notes.md"]
        try:
            for f in _tbox.glob("*.json"):
                f.unlink()
            # the flush tests above may have stamped proof-of-reply here
            (_tbox / ".last-posted").unlink(missing_ok=True)

            # Nobody asked it anything -> nothing to answer for.
            _tstamp.unlink(missing_ok=True)
            _run_hook({"cwd": _app_cwd})
            check("a turn nobody asked for is not announced", not list(_tbox.glob("*.json")))

            # Asked, did work, said nothing -> announce, and name the files.
            _tstamp.write_text(json.dumps({"session": "demo-app.app", "ts": _t2.time() - 5}),
                               encoding="utf-8")
            _run_hook({"cwd": _app_cwd})
            posted = [json.loads(f.read_text(encoding="utf-8")) for f in _tbox.glob("*.json")]
            check("a silent finish IS announced", len(posted) == 1)
            check("...and names the file the owner was waiting for",
                  posted and "slides.pdf" in posted[0]["text"])
            check("the turn stamp is consumed, so it announces once and not again",
                  not _tstamp.is_file())
            _run_hook({"cwd": _app_cwd})
            check("a second turn end does not repeat the announcement",
                  len(list(_tbox.glob("*.json"))) == 1)

            # A desk that already replied does not need a nag on top.
            for f in _tbox.glob("*.json"):
                f.unlink()
            _tstamp.write_text(json.dumps({"session": "demo-app.app", "ts": _t2.time() - 5}),
                               encoding="utf-8")
            (_tbox / "own-reply.json").write_text(json.dumps({"text": "done, here it is"}),
                                                  encoding="utf-8")
            _run_hook({"cwd": _app_cwd})
            check("a desk that already reported is left alone",
                  len(list(_tbox.glob("*.json"))) == 1)

            # ...and one whose reply was ALREADY POSTED (and therefore deleted
            # by the watchdog) is left alone too. First live run, 2026-08-01:
            # the flush outran the turn end and the announcer cried "posted
            # nothing" straight after the desk posted.
            for f in _tbox.glob("*.json"):
                f.unlink()
            (_tbox / ".last-posted").write_text("2026-08-01T19:40:59Z", encoding="utf-8")
            _tstamp.write_text(json.dumps({"session": "demo-app.app", "ts": _t2.time() - 5}),
                               encoding="utf-8")
            _run_hook({"cwd": _app_cwd})
            check("a reply the watchdog already posted still counts as having spoken",
                  not list(_tbox.glob("*.json")))
            (_tbox / ".last-posted").unlink()
            _fl_src = (real_root / "tools" / "discord" / "watchdog.py").read_text(encoding="utf-8")
            check("the watchdog stamps .last-posted at every post (the proof-of-reply)",
                  '".last-posted"' in _fl_src)

            # A read-only turn is not news. Owner, 2026-08-03: only issues.
            for f in _tbox.glob("*.json"):
                f.unlink()
            teh.changed_files = lambda cwd, since=None: []
            _tstamp.write_text(json.dumps({"session": "demo-app.app", "ts": _t2.time() - 5}),
                               encoding="utf-8")
            _run_hook({"cwd": _app_cwd})
            check("a turn that changed nothing says nothing at all",
                  not list(_tbox.glob("*.json")))

            # git is the oracle precisely because it honours .gitignore.
            check("changed_files asks git, so node_modules/dist cannot drown the signal",
                  "status" in (real_root / "tools" / "discord" / "turn_end_hook.py")
                  .read_text(encoding="utf-8") and "porcelain" in
                  (real_root / "tools" / "discord" / "turn_end_hook.py").read_text(encoding="utf-8"))
        finally:
            teh.changed_files = _real_changed
            for f in _tbox.glob("*.json"):
                f.unlink()
            _tstamp.unlink(missing_ok=True)

        # (The heal-fast-path and deaf-orchestrator suites lived here until the
        # run model deleted the machinery they exercised.)

        # Killing a desk clears its busy stamp and run lease too - otherwise a
        # freshly killed desk would look mid-turn or mid-run forever.
        wd_src_teh = (real_root / "tools" / "discord" / "watchdog.py").read_text(encoding="utf-8")
        _kbody = wd_src_teh[wd_src_teh.index("def kill_session"):]
        _kbody = _kbody[:_kbody.index("\ndef ", 10)]
        check("kill_session clears the busy stamp", '.busy"' in _kbody)
        check("kill_session clears the run lease", 'RUNS / f"{session}.json"' in _kbody)

        # Registered where new desks will actually inherit it. Since 2026-08-14
        # that is NOT the tracked template - a hook command is an absolute path
        # on one machine, and the template travels to every other one. What a
        # stamped project inherits instead is fix_hook_paths writing the hooks
        # into each component's own .claude\settings.local.json, which
        # fleet_ops runs at the end of every stamp.
        import fix_hook_paths as _fhp2
        _generated = _fhp2.hooks_block()
        stop = _generated.get("Stop", [])
        check("a stamped desk gets the Stop hook", "turn_end_hook.py" in json.dumps(stop))
        # Depth-relative paths were abandoned 2026-08-02: one settings file is
        # loaded by sessions at different depths, so no spelling is right for
        # all of them. CLAUDE_PROJECT_DIR resolves to the SESSION CWD - for a
        # spawned desk that is the COMPONENT folder, three levels below the
        # root. Proven live 2026-08-01: at ../../ the UserPromptSubmit hook
        # could not find its script, and a failing prompt hook BLOCKS the
        # prompt - a tool desk died in 3 seconds and the orchestrator answered
        # instead ("not you again"). Absolute is the only spelling that works,
        # which is why it is generated per machine and never committed.
        check("the generated hook path is absolute, not depth-relative",
              "${CLAUDE_PROJECT_DIR}" not in json.dumps(stop))
        check("...and points at a file that exists",
              all(pathlib.Path(h["command"].split('"')[1]).is_file()
                  for b in stop for h in b["hooks"]))
        check("...and the template itself ships none, so a clone inherits no path",
              "hooks" not in json.loads(
                  (real_root / "templates" / "project" / ".claude" / "settings.json")
                  .read_text(encoding="utf-8")))
        check("stamping a project wires its desks (fleet_ops runs the writer)",
              "fix_hook_paths.py" in (real_root / "tools" / "orchestrator" / "fleet_ops.py")
              .read_text(encoding="utf-8"))

        # The OTHER half of the busy/idle pair. Without UserPromptSubmit the
        # watchdog cannot tell "terminal mid-turn" from "terminal idle", and a
        # headless --continue during a live turn is two writers on one
        # conversation.
        ups = _generated.get("UserPromptSubmit", [])
        check("a stamped desk registers the UserPromptSubmit hook",
              bool(ups) and "turn_start_hook.py" in json.dumps(ups))
        check("...absolute as well",
              "${CLAUDE_PROJECT_DIR}" not in json.dumps(ups)
              and all(pathlib.Path(h["command"].split('"')[1]).is_file()
                      for b in ups for h in b["hooks"]))
        rootset = json.loads((real_root / ".claude" / "settings.local.json")
                             .read_text(encoding="utf-8")) \
            if (real_root / ".claude" / "settings.local.json").is_file() else {}
        check("the root workspace has the UserPromptSubmit hook written locally",
              "turn_start_hook.py" in json.dumps(rootset.get("hooks", {})
                                                 .get("UserPromptSubmit", [])))

        import turn_start_hook as tsh
        tsh.ROOT, tsh.TURNS = SAND, wd.TURNS

        def _run_start_hook(payload):
            out2 = _io.StringIO()
            old2 = sys.stdin
            sys.stdin = _io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))
            try:
                with _ctx.redirect_stdout(out2):
                    rc2 = tsh.main()
            finally:
                sys.stdin = old2
            return rc2, out2.getvalue()

        rc, out = _run_start_hook({"cwd": _app_cwd})
        check("prompt-submit: busy stamp written, nothing printed",
              rc == 0 and out == "" and (wd.TURNS / "demo-app.app.busy").is_file())
        (wd.TURNS / "demo-app.app.busy").unlink()
        rc, out = _run_start_hook({"cwd": "C:\\Windows"})
        check("prompt-submit outside a desk does nothing", rc == 0 and out == "")
        rc, out = _run_start_hook("{ nope")
        check("prompt-submit survives garbage stdin", rc == 0)

        # -- identity that survives a cd ---------------------------------------
        # 2026-08-12, twice in one day: a project's web desk cd'd from its component
        # folder up to the project root to create its repo (constitution par.7
        # forbids `git -C`, so a desk that must touch its repo root walks
        # there). UserPromptSubmit stamped the component id; Stop asked the
        # CURRENT cwd, got two path parts where it wanted three, resolved None
        # and cleared NOTHING. The stamp outlived its turn - which tells the
        # bridge "do not nudge" and the watchdog "do not run" - and his mail sat
        # in the inbox of an idle desk for 35 minutes. The deaf-desk recency
        # check above is the ALARM for that state; these are the cause.
        print("== turn identity survives a session changing folder ==")
        import desk_identity as di
        di.ROOT, di.TURNS = SAND, wd.TURNS
        _repo_root = str(SAND / "projects" / "demo-app")      # walked here for git
        _busy_app = wd.TURNS / "demo-app.app.busy"

        check("identity: a cwd encodes to Claude Code's conversation-folder name",
              di.encode_project_dir("W:\\omnius\\projects\\some-app\\web")
              == "W--omnius-projects-some-app-web")

        _busy_app.unlink(missing_ok=True)
        _run_start_hook({"cwd": _app_cwd, "session_id": "conv-walk"})
        check("the busy stamp records the conversation that wrote it",
              json.loads(_busy_app.read_text(encoding="utf-8")).get("claudeSession")
              == "conv-walk")
        rc, out = _run_hook({"cwd": _repo_root, "session_id": "conv-walk"})
        check("a turn that ENDS from the repo root still clears its own stamp",
              rc == 0 and out == "" and not _busy_app.is_file())

        # Worse than failing to clear your own stamp is clearing someone else's:
        # a desk that walks into tools\discord to fix a tool would take down
        # tool.discord's stamp and leave its own standing - deafening two desks
        # with one turn.
        _busy_tool = wd.TURNS / "tool.discord.busy"
        _run_start_hook({"cwd": _app_cwd, "session_id": "conv-walk2"})
        _busy_tool.write_text(json.dumps({"session": "tool.discord",
                                          "claudeSession": "conv-elsewhere"}), encoding="utf-8")
        _run_hook({"cwd": str(SAND / "tools" / "discord"), "session_id": "conv-walk2"})
        check("...and it clears its OWN stamp, not the one the folder belongs to",
              not _busy_app.is_file() and _busy_tool.is_file())
        _busy_tool.unlink()

        # The stamp is the exact answer. The conversation FILE is the fallback
        # for a turn whose very first prompt already ran from the wrong folder,
        # because Claude Code names that file's folder after the cwd the session
        # STARTED in - and a desk always starts in its own.
        _conv = str(SAND / "conv" / di.encode_project_dir(
            SAND / "projects" / "demo-app" / "app") / "c.jsonl")
        check("identity: the conversation folder names the desk",
              di.desk_from_transcript(_conv) == "demo-app.app")
        _run_start_hook({"cwd": _repo_root, "session_id": "conv-fresh",
                         "transcript_path": _conv})
        check("a turn that BEGINS outside the desk folder is still stamped",
              _busy_app.is_file())
        _run_hook({"cwd": _repo_root, "session_id": "conv-fresh"})
        check("...and that stamp comes down at the end of it too",
              not _busy_app.is_file())

        check("identity: an unknown conversation folder is nobody's desk",
              di.desk_from_transcript(str(SAND / "conv" / "W--elsewhere" / "c.jsonl")) is None)
        # Ambiguity is refused, never guessed: `demo\app-x` and `demo-app\x`
        # encode to the same name, and a wrong desk clears a stamp that is not
        # its own - the failure two paragraphs up, arrived at from the fix.
        (SAND / "projects" / "demo" / "app-x").mkdir(parents=True, exist_ok=True)
        (SAND / "projects" / "demo-app" / "x").mkdir(parents=True, exist_ok=True)
        try:
            check("identity: two desks with one encoding resolve to neither",
                  di.desk_from_transcript(str(SAND / "conv" / di.encode_project_dir(
                      SAND / "projects" / "demo" / "app-x") / "c.jsonl")) is None)
        finally:
            shutil.rmtree(SAND / "projects" / "demo", ignore_errors=True)
            shutil.rmtree(SAND / "projects" / "demo-app" / "x", ignore_errors=True)

        # A stamp written before 2026-08-12 carries no conversation id, and a
        # missing id must never read as a wildcard: matching one would clear a
        # stranger's stamp on every single turn end.
        _legacy = wd.TURNS / "legacy.desk.busy"
        _legacy.write_text(json.dumps({"session": "legacy.desk"}), encoding="utf-8")
        check("identity: a stamp from before this existed matches nobody",
              di.desk_from_live_turn(None) is None
              and di.desk_from_live_turn("conv-walk") is None)
        _legacy.unlink()

        # The relay is the third reader of this identity, and the one where
        # getting it wrong costs the most: resolve None and it escalates
        # nothing, leaving the desk on a local dialog nobody can see - the exact
        # failure the whole escalation feature exists to prevent.
        check("the permission relay resolves identity the same way",
              "desk_for_event" in (real_root / "tools" / "discord"
                                   / "permission_relay.py").read_text(encoding="utf-8"))
    finally:
        (wd.SESSIONS / "demo-app.app.json").unlink(missing_ok=True)
        for _f in wd.TURNS.glob("*.busy"):
            _f.unlink(missing_ok=True)

    # == no stray console windows ==============================================
    # The watchdog runs under pythonw, so it has NO console - and every console
    # child it starts therefore gets a window of its own. child_counts() shells
    # out to powershell every 20s for the board, which put a window flashing on
    # the owner's desktop twice a minute before anyone connected the two
    # (2026-08-01: "what is the terminal window popping up every 30 sec?").
    # Structural, not remembered: any new helper must opt out too.
    print("== no stray console windows ==")
    import ast as _ast

    def _console_children(path, exempt_funcs):
        """-> [(func, line)] for subprocess calls that would open a window."""
        tree = _ast.parse(path.read_text(encoding="utf-8"))
        bad, exempt_lines = [], set()
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and node.name in exempt_funcs:
                for sub in _ast.walk(node):
                    exempt_lines.add(getattr(sub, "lineno", -1))
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.Call):
                continue
            f = node.func
            if not (isinstance(f, _ast.Attribute) and f.attr in ("run", "Popen")
                    and isinstance(f.value, _ast.Name) and f.value.id == "subprocess"):
                continue
            if node.lineno in exempt_lines:
                continue
            if not any(k.arg == "creationflags" for k in node.keywords):
                bad.append((f.attr, node.lineno))
        return bad

    # open_tab is the ONE exemption: its visible window is the owner's explicit
    # ask ("normal terminal windows", 2026-08-01). Everything else - including
    # the headless claude runs - stays windowless.
    _wdpath = real_root / "tools" / "discord" / "watchdog.py"
    _leaks = _console_children(_wdpath, exempt_funcs={"open_tab"})
    check("every console child of the watchdog is windowless (except open_tab)",
          _leaks == [], f"missing creationflags at {_leaks}")
    check("open_tab really does open a window (it is the desk you sit at)",
          _console_children(_wdpath, exempt_funcs=set()) != [])
    _iwleaks = _console_children(real_root / "tools" / "discord" / "inbox_watch.py",
                                 exempt_funcs=set())
    check("the check-in opens no window", _iwleaks == [],
          f"missing creationflags at {_iwleaks}")
    check("NO_WINDOW degrades to 0 off Windows rather than crashing the import",
          "getattr(subprocess" in _wdpath.read_text(encoding="utf-8"))

    # == the no-Windows-Terminal path opens a window, not an error dialog =====
    # Windows Terminal does not ship with Windows 10, so the fallback IS the
    # normal branch there - and it went through `cmd /c start <title>`. `start`
    # reads its first unquoted token as the PROGRAM to run, and an argv list
    # never quotes a bare word, so a one-word desk name asked Windows to run a
    # program called "orchestrator". First boot of a stock Win10 VM, 2026-08-15:
    # an error dialog and no desk. A title WITH spaces was quoted by accident,
    # which is why only single-word desks broke.
    for _f, _src in (("watchdog.py", _wdpath.read_text(encoding="utf-8")),
                     ("fleet_ops.py", (real_root / "tools" / "orchestrator" / "fleet_ops.py")
                      .read_text(encoding="utf-8"))):
        check(f"{_f}: no desk window is opened through `cmd /c start`",
              '"start"' not in _src, "an unquoted title is read as a command")
        check(f"{_f}: the wt-less branch asks for a console directly",
              "CREATE_NEW_CONSOLE" in _src and "NEW_CONSOLE" in _src)
        check(f"{_f}: ...and hands cmd a command STRING, not a list",
              "cmd /c {inner}" in _src or "cmd /k {inner}" in _src,
              "Python quotes list elements with \\\" , which cmd does not read as an escape")

    # And the same thing PROVEN, because asserting on source text is how the
    # first attempt at this fix passed review and then failed on the VM:
    #   python: can't open file 'C:\...\omnius-agent\"C:\...\desk_bridge.py"'
    # The path is quoted inside a single argv element, Python escapes those
    # quotes for a C runtime, cmd reads them literally, and the whole thing
    # becomes a relative path. A directory with spaces is deliberate here.
    if _os.name == "nt":
        _qdir = SAND / "desk dir with spaces"
        _qdir.mkdir(parents=True, exist_ok=True)
        _fake = _qdir / "desk_bridge.py"
        _fake.write_text("import sys\nprint('ARGV', len(sys.argv), sys.argv[1])\n",
                         encoding="utf-8")
        _inner = f'python "{_fake}" orchestrator --model opus --effort xhigh'
        _r = subprocess.run(f'cmd /c {_inner} || pause', capture_output=True,
                            text=True, cwd=str(_qdir), timeout=60)
        check("a desk window's command survives cmd, quoted path and all",
              "ARGV 6 orchestrator" in (_r.stdout + _r.stderr),
              (_r.stdout + _r.stderr).strip()[:160])
    # The VISIBLE terminal is now the human verb - fleet_ops.open_desk.
    fo_src_w = (real_root / "tools" / "orchestrator" / "fleet_ops.py").read_text(encoding="utf-8")
    check("the visible terminal verb moved to fleet_ops.open_desk",
          "def open_desk" in fo_src_w and '"new-tab"' in fo_src_w)

    print("== backlog notices ==")
    import time as _time   # _os already imported above
    box = wd.INBOX / "demo-app.app"
    box.mkdir(parents=True, exist_ok=True)
    for f in box.glob("*.json"):
        f.unlink()
    wd._backlog_notified.clear()
    sent.clear()

    env = box / "999.json"
    # `from` is present on every envelope the watchdog writes; since desk mail
    # (D2) the backlog notice is for HUMAN mail only, so the fixture says whose.
    env.write_text(json.dumps({"id": "999", "from": "owner",
                               "channelId": "C-app", "text": "hola"}), encoding="utf-8")
    n, oldest = wd.inbox_backlog("demo-app.app")
    check("inbox_backlog counts a queued envelope", n == 1 and oldest >= 0)
    check("inbox_backlog is 0 for a desk with no box", wd.inbox_backlog("nope.nope") == (0, 0.0))

    wd.check_backlogs()
    check("a fresh envelope is NOT announced (no crying wolf)", sent == [])

    old_t = _time.time() - 200
    _os.utime(env, (old_t, old_t))
    wd.check_backlogs()
    check("an envelope left 200s is announced", len(sent) == 1)
    check("the notice goes to the envelope's OWN channel", sent[-1][0] == "C-app")
    check("the notice names the desk and how long", "demo-app.app" in sent[-1][1] and "3m" in sent[-1][1])

    wd.check_backlogs()
    check("the same envelope is never announced twice", len(sent) == 1)

    # A run actively working must not be described as asleep - "will answer
    # when it wakes" next to a working run read as a broken system (2026-08-01).
    env2b = box / "997.json"
    env2b.write_text(json.dumps({"id": "997", "from": "owner",
                                 "channelId": "C-app", "text": "task"}), encoding="utf-8")
    _os.utime(env2b, (old_t, old_t))
    wd.RUNNING["demo-app.app"] = FakeProc()
    sent.clear()
    wd.check_backlogs()
    check("a desk with a run in progress says NOTHING - the reply is the notification",
          sent == [])
    # ...but silence has a limit. 2026-08-12, after 20 minutes of real work with
    # his message queued behind it: "you got stuck, I had to restart it - a user
    # must never be left hanging." A working desk and a dead one are the same
    # thing on a phone, and he acted on that reading by killing healthy work.
    _os.utime(env2b, (_time.time() - wd.LONG_WORK_NOTICE_SECONDS - 60,) * 2)
    sent.clear()
    wd._backlog_notified.clear()
    wd.check_backlogs()
    check("a run working THIS long says so once - the silence is what he reads as dead",
          len(sent) == 1 and "still working" in sent[-1][1] and "!stop" in sent[-1][1])
    wd.check_backlogs()
    check("...once per message, never a progress bar", len(sent) == 1)
    # An ack already explained the wait; a second voice on top is the info-noise
    # he banned on 2026-08-03.
    _box_out = wd.OUTBOX / "demo-app.app"
    _box_out.mkdir(parents=True, exist_ok=True)
    (_box_out / ".last-posted").write_text("", encoding="utf-8")
    sent.clear()
    wd._backlog_notified.clear()
    wd.check_backlogs()
    check("...and not at all if the desk already acknowledged him", sent == [])
    (_box_out / ".last-posted").unlink(missing_ok=True)
    wd.RUNNING.pop("demo-app.app", None)
    env2b.unlink()
    wd._backlog_notified.clear()

    env.unlink()
    wd.check_backlogs()
    check("handled envelopes are forgotten (the set cannot grow forever)",
          wd._backlog_notified == set())

    # An envelope with no channelId must not crash the pass - it just cannot be
    # announced anywhere, and one bad file must never wedge the loop.
    bad = box / "998.json"
    bad.write_text(json.dumps({"id": "998", "text": "no channel"}), encoding="utf-8")
    _os.utime(bad, (old_t, old_t))
    sent.clear()
    wd.check_backlogs()
    check("an envelope without channelId is skipped, not fatal", sent == [])
    bad.write_text("{ not json", encoding="utf-8")
    _os.utime(bad, (old_t, old_t))
    wd.check_backlogs()
    check("a corrupt envelope is skipped, not fatal", sent == [])
    bad.unlink()
    wd._backlog_notified.clear()

    src_wd2 = (real_root / "tools" / "discord" / "watchdog.py").read_text(encoding="utf-8")
    # Assert it is CALLED in the loop, not that it sits beside a particular
    # neighbour. The first version pinned adjacency and went red the moment
    # heal_deaf_desks was inserted between them - a test failing for
    # bookkeeping rather than for behaviour.
    # Anchor on the INDENTED call, not the bare name: index() would otherwise
    # find "def flush_outboxes(mapping):" and slice the wrong region - the
    # same mistake as the earlier "except api.ApiError" slice.
    call_site = " " * 12 + "flush_outboxes(mapping)"
    loop_tail = src_wd2[src_wd2.index(call_site):]
    check("check_backlogs actually runs in the poll loop",
          "check_backlogs()" in loop_tail[:600])

    print("== fleet.json ==")
    check("role_of: orchestrator", wd.role_of("orchestrator") == "orchestrator")
    check("role_of: daybook", wd.role_of("daybook") == "daybook")
    check("role_of: tool.<name>", wd.role_of("tool.discord") == "tool")
    check("role_of: project.component", wd.role_of("demo-proj.app") == "project")

    _real_cfg = wd.FLEET_CFG
    # The shipped defaults, not a fixture - and since 2026-08-14 what SHIPS is
    # the example: config\* is gitignored, install copies it to fleet.json.
    # So test the EXAMPLE first. The machine's own fleet.json is instance
    # state - `!model` exists to mutate it, and the owner using a shipped verb
    # must never fail the shipped-defaults check (found 2026-08-16: this desk
    # ran fable/max by his hand and the suite blamed the product).
    wd.FLEET_CFG = real_root / "config" / "fleet.example.json"
    if not wd.FLEET_CFG.is_file():
        wd.FLEET_CFG = real_root / "config" / "fleet.json"
    try:
        orch = wd.desk_config("orchestrator")
        proj = wd.desk_config("demo-proj.democomp")
        check("orchestrator does NOT bypass (keeps its rails)", orch["permissionMode"] is None)
        # Reversed 2026-08-01: bypassPermissions HANGS an interactive spawn.
        check("no desk bypasses - it hangs interactive spawns", proj["permissionMode"] is None)
        check("daybook does not bypass either", wd.desk_config("daybook")["permissionMode"] is None)
        check("shipped defaults are opus/xhigh for every desk",
              orch["model"] == "opus" and orch["effort"] == "xhigh"
              and proj["model"] == "opus" and proj["effort"] == "xhigh")

        # The latency decision rides in config, so it must be pinned: the
        # orchestrator boots fresh (11 MB transcript vs 52 KB of actual chat),
        # component desks keep transcript resume (their build context is real).
        check("orchestrator runs boot FRESH (the 200x context-tax fix)",
              orch["resume"] == "fresh")
        check("project desks keep transcript resume", proj["resume"] == "transcript")
        # Owner, 2026-08-01: "I want them normal terminal windows, so when I am
        # at the desk I can work directly in the claude CLI and not Discord."
        # Owner's model, 2026-08-03: Discord mail OPENS that desk's bridge and
        # it stays warm, because he must be as efficient from Discord as at the
        # keyboard. Boot opens nothing; he starts native desks himself.
        check("a Discord message opens a warm bridge for that desk",
              proj["window"] == "terminal" and orch["window"] == "terminal")
        _as2 = (_rr2 / "tools" / "discord" / "autostart.ps1").read_text(encoding="utf-8")
        check("...but nothing opens a desk at boot",
              "desks open on demand, never at boot" in _as2
              and "Register-OmniusDesk" not in _as2.split("--- desks are NOT started")[1])

        # A broken config must never stop a spawn - shipped behaviour is the floor.
        wd.FLEET_CFG = SAND / "does-not-exist.json"
        check("missing fleet.json falls back to built-in defaults",
              wd.desk_config("x.y") == {"model": "opus", "effort": "xhigh",
                                        "permissionMode": None, "resume": "transcript",
                                        "window": "headless"})
        bad = SAND / "bad-fleet.json"; bad.write_text("{ not json", encoding="utf-8")
        wd.FLEET_CFG = bad
        check("corrupt fleet.json falls back instead of wedging the fleet",
              wd.desk_config("x.y")["model"] == "opus")

        # Resolution is not the command line. Assert the flag actually reaches claude,
        # and - just as important - that it does NOT reach the orchestrator.
        wd.FLEET_CFG = real_root / "config" / "fleet.json"
        _rp, _rw = _sp.Popen, _sh.which
        cap = []
        _sp.Popen = lambda args, **kw: (cap.append(args), FakeProc())[1]
        _sh.which = lambda n: f"C:\\fake\\{n}.exe"
        try:
            cap.clear(); _real_start_run("demo-app.app")
            proj_cmd = " ".join(cap[-1]) if cap else ""
            check("a project run carries NO --permission-mode (profile + relay are the rails)",
                  "--permission-mode" not in proj_cmd)
            cap.clear(); _real_start_run("orchestrator")
            orch_cmd = " ".join(cap[-1]) if cap else ""
            check("the orchestrator run carries NO --permission-mode",
                  "--permission-mode" not in orch_cmd)
        finally:
            _sp.Popen, _sh.which = _rp, _rw
            wd.RUNNING.clear()
            for f in wd.RUNS.glob("*.json"):
                f.unlink()
    finally:
        wd.FLEET_CFG = _real_cfg

    # An unknown mode must be DROPPED, not passed through: an invalid --permission-mode
    # kills the spawn at argument parsing, with the terminal closing too fast to read.
    src_wd = (real_root / "tools" / "discord" / "watchdog.py").read_text(encoding="utf-8")
    check("unknown permissionMode is validated before reaching the CLI",
          "VALID_PERMISSION_MODES" in src_wd and "mode in VALID_PERMISSION_MODES" in src_wd)

    print("== reload loop regression ==")
    wd_src = (real_root / "tools" / "discord" / "watchdog.py").read_text(encoding="utf-8")
    # The ordering rule now lives in deliver(), which BOTH transports call - the
    # gateway drain and the REST sweep. That is the point of it being one
    # function: the rule cannot hold on one path and quietly lapse on the other.
    # (Behavioural proof is in the gateway section below; this pins the source so
    # the two lines cannot be swapped back without a test going red.)
    _at = wd_src.index("def deliver(")
    loop_body = wd_src[_at:wd_src.index("def drain_gateway(", _at)]
    check("watchdog: cursor persisted BEFORE handle_message (no !reload re-exec loop)",
          "persist()" in loop_body
          and loop_body.index("persist()") < loop_body.index("handle_message("))
    check("both transports deliver through the one function that enforces it",
          "deliver(m, cid, target" in wd_src[wd_src.index("def drain_gateway("):]
          and wd_src.count("deliver(m, cid, target") >= 2)

    print("== status_banner ==")
    import os as _os
    check("pid_alive: own pid alive", sb.pid_alive(_os.getpid()))
    check("pid_alive: bogus pid dead", not sb.pid_alive(99999999))
    check("pid_alive: None dead", not sb.pid_alive(None))
    sb.STATE = SAND  # sandboxed probes: no lock, no sessions dir contents

    # The banner is the other surface a human trusts. If only !status learned to
    # tell alive from listening, the blind spot just moves here.
    (SAND / "permissions").mkdir(parents=True, exist_ok=True)
    check("banner: no stall markers -> nothing reported", sb.stalled_sessions() == [])
    (SAND / "permissions" / "demo-app.app.stalled").write_text("{}", encoding="utf-8")
    check("banner: a stalled desk is listed", sb.stalled_sessions() == ["demo-app.app"])
    check("banner: the .stalled suffix is stripped from the id",
          not any(s.endswith(".stalled") for s in sb.stalled_sessions()))
    check("banner: STALLED is rendered, not merely computed",
          "STALLED" in "\n".join(sb.render()) if isinstance(sb.render(), list) else "STALLED" in sb.render())
    (SAND / "permissions" / "demo-app.app.stalled").unlink()
    up, detail = sb.probe_watchdog()
    check("probe_watchdog: no lock -> down", not up and "not running" in detail)
    (SAND / "watchdog").mkdir(exist_ok=True)
    (SAND / "watchdog" / "lock.json").write_text(
        json.dumps({"pid": 99999999, "startedAt": "x", "machine": "t"}), encoding="utf-8")
    up, detail = sb.probe_watchdog()
    check("probe_watchdog: dead pid -> stale", not up and "stale" in detail)
    (SAND / "watchdog" / "lock.json").write_text(
        json.dumps({"pid": _os.getpid(), "startedAt": "x", "machine": "t"}), encoding="utf-8")
    # A live pid alone is NOT up any more: the process can hold its lock and log
    # while delivering nothing. Only a fresh beacon counts as listening.
    up, detail = sb.probe_watchdog()
    check("probe_watchdog: live pid but no beacon -> not up", not up and "beacon" in detail)
    beacon = SAND / "watchdog" / "beacon.json"
    beacon.write_text(json.dumps({"at": now(-5), "channels": 7, "machine": "t"}), encoding="utf-8")
    up, _ = sb.probe_watchdog()
    check("probe_watchdog: live pid + fresh beacon -> up", up)
    beacon.write_text(json.dumps({"at": now(-600), "channels": 7, "machine": "t"}), encoding="utf-8")
    up, detail = sb.probe_watchdog()
    check("probe_watchdog: stale beacon -> alive but not polling",
          not up and "NOT POLLING" in detail)
    beacon.write_text(json.dumps({"at": now(-5), "channels": 7}), encoding="utf-8")
    claim("bannertest", pid=_os.getpid())
    check("live_sessions: a claim with a LIVE pid is listed", "bannertest" in sb.live_sessions())
    claim("bannertest", pid=99999997)
    check("live_sessions: a dead pid is NOT listed, however fresh lastSeenAt looks",
          "bannertest" not in sb.live_sessions())
    (SAND / "watchdog" / "runs").mkdir(parents=True, exist_ok=True)
    (SAND / "watchdog" / "runs" / "runnertest.json").write_text(
        json.dumps({"session": "runnertest", "pid": _os.getpid()}), encoding="utf-8")
    check("live_sessions: an active headless run is listed as a run",
          "runnertest (run)" in sb.live_sessions())
    (SAND / "watchdog" / "runs" / "runnertest.json").unlink()
    (wd.SESSIONS / "bannertest.json").unlink(missing_ok=True)
    out = sb.render()
    check("render: returns a banner", "O M N I U S" in out and "Watchdog" in out)
    check("render: encoding-safe (cp1252)", out == out.encode("cp1252", "replace").decode("cp1252"))

    # == .env parsing =========================================================
    # A .env is hand-edited, pasted into, and written by PowerShell (UTF-16 by
    # default). Every shape below silently broke a detector at some point.
    print("== .env parsing ==")
    GOOD = "111111111111111111"
    envdir = SAND / "envcases"
    def parsed(name, data, key="DISCORD_GUILD_ID"):
        d = envdir / name
        d.mkdir(parents=True, exist_ok=True)
        (d / ".env").write_bytes(data)
        saved = api.ROOT
        try:
            api.ROOT = d
            return api.load_env().get(key)
        finally:
            api.ROOT = saved

    check("load_env: utf-16 (PowerShell '>' default)", parsed("u16", f"DISCORD_GUILD_ID={GOOD}\n".encode("utf-16")) == GOOD)
    check("load_env: utf-8 BOM (Notepad)", parsed("bom", f"﻿DISCORD_GUILD_ID={GOOD}\n".encode("utf-8")) == GOOD)
    check("load_env: quoted value", parsed("q", f'DISCORD_GUILD_ID="{GOOD}"\n'.encode()) == GOOD)
    check("load_env: inline comment", parsed("c", f"DISCORD_GUILD_ID={GOOD} # my server\n".encode()) == GOOD)
    check("load_env: lowercase key", parsed("lc", f"discord_guild_id={GOOD}\n".encode()) == GOOD)
    check("load_env: export prefix", parsed("ex", f"export DISCORD_GUILD_ID={GOOD}\n".encode()) == GOOD)
    check("load_env: CRLF", parsed("crlf", f"DISCORD_GUILD_ID={GOOD}\r\n".encode()) == GOOD)
    check("load_env: comment line ignored", parsed("cm", f"# DISCORD_GUILD_ID=nope\nDISCORD_GUILD_ID={GOOD}\n".encode()) == GOOD)
    # U+2028 must NOT act as a line break, or api.py and PowerShell disagree on
    # how many lines the file has. It is left in the value and rejected below.
    check("load_env: U+2028 is not a line break",
          parsed("ls", f"DISCORD_GUILD_ID={GOOD} X=1\n".encode("utf-8")) != GOOD)

    # == config validation ====================================================
    # Presence is not validity - a wrong-but-non-empty id used to pass every
    # check and then kill the watchdog with no usable error.
    print("== config validation ==")
    def problems(tok, guild, owner):
        t, g, o = api.TOKEN, api.GUILD, api.OWNER
        try:
            api.TOKEN, api.GUILD, api.OWNER = tok, guild, owner
            return api.config_problems()
        finally:
            api.TOKEN, api.GUILD, api.OWNER = t, g, o

    check("config_problems: all good -> none", problems("tok", GOOD, "9" * 18) == [])
    check("config_problems: empty token", any("BOT_TOKEN" in p for p in problems("", GOOD, "9" * 18)))
    check("config_problems: short guild id", any("GUILD_ID" in p for p in problems("t", "123", "9" * 18)))
    check("config_problems: non-numeric owner id", any("OWNER_ID" in p for p in problems("t", GOOD, "abc")))
    check("config_problems: quote-contaminated id rejected",
          any("GUILD_ID" in p for p in problems("t", f'"{GOOD}"', "9" * 18)))
    def require_ok(tok, guild, owner):
        t, g, o = api.TOKEN, api.GUILD, api.OWNER
        try:
            api.TOKEN, api.GUILD, api.OWNER = tok, guild, owner
            api.require_config()
            return True
        except api.ApiError:
            return False
        finally:
            api.TOKEN, api.GUILD, api.OWNER = t, g, o

    check("require_config: tolerates a missing owner id (admin CLI needs token+guild)",
          require_ok("t", GOOD, "") and problems("t", GOOD, "") != [])
    check("require_config: still rejects a malformed guild id", not require_ok("t", "bad", "9" * 18))

    # == crash-safety =========================================================
    print("== crash-safety ==")
    aj = SAND / "atomic.json"
    wd.write_json_atomic(aj, {"a": 1})
    check("write_json_atomic: writes", json.loads(aj.read_text(encoding="utf-8")) == {"a": 1})
    check("write_json_atomic: leaves no .tmp", not list(SAND.glob("atomic.json.tmp")))
    wd.WD_STATE = SAND / "watchdog"        # never touch the real lock from a test
    wd.WD_STATE.mkdir(parents=True, exist_ok=True)
    (wd.WD_STATE / "lock.json").write_text("{}", encoding="utf-8")
    wd.release_lock()
    check("release_lock: removes the lock", not (wd.WD_STATE / "lock.json").exists())
    wd.release_lock()  # must be safe twice - crash paths may double-call
    check("release_lock: idempotent", True)
    wd.write_beacon(7)
    b = json.loads((wd.WD_STATE / "beacon.json").read_text(encoding="utf-8"))
    check("write_beacon: records a good pass", b["channels"] == 7 and b["at"].endswith("Z"))
    biglog = wd.LOGS / "watchdog.log"
    biglog.parent.mkdir(parents=True, exist_ok=True)
    biglog.write_text("x" * 200, encoding="utf-8")
    wd.rotate_log(max_bytes=50)
    check("rotate_log: oversized log rolls to .1",
          (wd.LOGS / "watchdog.log.1").exists() and not biglog.exists())
    biglog.write_text("small", encoding="utf-8")
    wd.rotate_log(max_bytes=1_000_000)
    check("rotate_log: small log left alone", biglog.exists())

    # == scheduled envelopes ==================================================
    # Heartbeat is the batched approximate loop; this is exact timing and
    # one-shots. The catch-up policy is the part worth pinning down: waking to
    # a backlog of stale reminders is worse than missing them.
    print("== schedule ==")
    import schedule as sch
    sch.SCHEDULE = SAND / "schedule"
    sch.JOBS = sch.SCHEDULE / "jobs.json"
    D = datetime

    check("schedule: rejects a bad --every", _raises(sch.parse_every, "20x"))
    check("schedule: rejects a bad --daily", _raises(sch.parse_daily, "25:00"))
    check("schedule: parses 2h", sch.parse_every("2h") == timedelta(hours=2))

    base = D(2026, 7, 25, 10, 0, 0)
    nxt = sch.next_run({"kind": "daily", "daily": "07:00"}, base)
    check("schedule: daily 07:00 rolls to tomorrow when past", nxt == D(2026, 7, 26, 7, 0))
    nxt = sch.next_run({"kind": "daily", "daily": "18:30"}, base)
    check("schedule: daily 18:30 fires later today", nxt == D(2026, 7, 25, 18, 30))
    # 2026-07-25 is a Saturday; weekdays must skip to Monday
    nxt = sch.next_run({"kind": "daily", "daily": "07:00", "weekdays": True}, base)
    check("schedule: weekdays skips the weekend", nxt.weekday() == 0)
    check("schedule: expired one-shot returns None",
          sch.next_run({"kind": "at", "at": "2020-01-01T00:00:00"}, base) is None)

    # missed runs are skipped forward, not replayed
    j = {"kind": "every", "every": "20m", "nextRun": (base - timedelta(hours=5)).strftime(sch.FMT)}
    nxt = sch.next_run(j, base)
    check("schedule: 5h of missed runs collapse to one future slot",
          nxt > base and nxt <= base + timedelta(minutes=20))

    sch.save_jobs([])
    job = sch.add_job("every", "20m", "orchestrator", "check the deploy")
    check("schedule: add persists a job", len(sch.load_jobs()) == 1 and job["nextRun"])
    due, kept = sch.due_jobs(now=D.strptime(job["nextRun"], sch.FMT) + timedelta(seconds=1))
    check("schedule: job fires at its time", len(due) == 1)
    check("schedule: recurring job is rescheduled, not dropped",
          len(kept) == 1 and kept[0]["nextRun"] > job["nextRun"])
    sch.save_jobs([])
    one = sch.add_job("at", (D.now() + timedelta(minutes=1)).strftime(sch.FMT),
                      "orchestrator", "ship it")
    due, kept = sch.due_jobs(now=D.strptime(one["nextRun"], sch.FMT) + timedelta(seconds=1))
    check("schedule: one-shot fires once and is removed", len(due) == 1 and kept == [])
    sch.save_jobs([])

    # delivery goes through the ordinary envelope path
    wd.schedule = sch
    sch.save_jobs([{"id": "j1", "kind": "every", "every": "20m", "to": "demo-app.app",
                    "text": "scheduled hello", "weekdays": False,
                    "nextRun": (D.now() - timedelta(minutes=1)).strftime(sch.FMT)}])
    wd._last_schedule_check = 0
    claim("demo-app.app", lastSeenAt=now(-5))
    wd.fire_due_schedules()
    envs = list((wd.INBOX / "demo-app.app").glob("sched-*.json"))
    check("schedule: due job becomes an inbox envelope", len(envs) == 1)
    check("schedule: envelope marked as from the scheduler",
          json.loads(envs[0].read_text(encoding="utf-8"))["from"] == "schedule")
    check("schedule: job rescheduled after firing",
          D.strptime(sch.load_jobs()[0]["nextRun"], sch.FMT) > D.now())
    sch.save_jobs([])

    # == a skipped routine must not vanish ====================================
    # fire_due_schedules() used to `return` when nothing fired, which threw away
    # the very state due_jobs() had just computed for the jobs it DECLINED to
    # fire. A too-stale routine re-read the same stale nextRun on every pass:
    # stuck forever, miss counter pinned at 1 on disk, and invisible.
    print("== schedule: misses persist ==")
    def _stale(missed=0, mins=90):
        sch.save_jobs([{"id": "j2", "kind": "every", "every": "20m",   # grace 10m
                        "to": "demo-app.app", "text": "stale check",
                        "weekdays": False, "missed": missed,
                        "nextRun": (D.now() - timedelta(minutes=mins)).strftime(sch.FMT)}])
        wd._last_schedule_check = 0
        wd._last_jobs_written = None
        wd.fire_due_schedules()
        return sch.load_jobs()[0]

    before = len(list((wd.INBOX / "demo-app.app").glob("sched-*.json")))
    j = _stale()
    check("a too-stale routine does not fire",
          len(list((wd.INBOX / "demo-app.app").glob("sched-*.json"))) == before)
    check("but its miss IS written to disk", j.get("missed") == 1, f"got {j.get('missed')}")
    check("and its nextRun is fast-forwarded, not left stale",
          D.strptime(j["nextRun"], sch.FMT) > D.now())
    check("misses accumulate across passes", _stale(missed=4).get("missed") == 5)

    # == schedule: loops (docs\DELEGATION.md D5) ===============================
    # The continuation pattern, counted. Budget enforced at add-time (the desk
    # is told inside its own run) AND at fire-time (the belt, for hand-edited
    # jobs). Loops never self-extend.
    print("== schedule: loops ==")
    _real_sched_root, _real_lbd = sch.ROOT, sch.loop_budget_default
    _real_own = sch.own_session
    sch.ROOT = SAND                      # desk_cwd validates against the sandbox
    sch.LOOPS = SAND / "watchdog" / "loops"
    sch.loop_budget_default = lambda: 3  # deterministic, never this machine's config
    sch.own_session = lambda: "orchestrator"   # the suite's cwd wanders; pin it
    sch.save_jobs([])
    check("loop: --to is validated against real desks - a typo cannot invent one",
          sch.main(["add", "--in", "2m", "--to", "ghost.desk", "--text", "x"]) == 2)
    check("loop: an illegal id shape is refused with the grammar",
          sch.main(["add", "--in", "2m", "--to", "NotADesk", "--text", "x"]) == 2)
    check("loop: a self-addressed add without --loop is refused",
          sch.main(["add", "--in", "2m", "--to", "orchestrator", "--text", "x"]) == 2
          and not sch.load_jobs())
    check("loop: --max above the configured budget is refused",
          sch.main(["add", "--in", "2m", "--to", "orchestrator", "--loop", "auto",
                    "--max", "9", "--text", "x"]) == 2)
    check("loop: --loop auto opens a ledger and queues the continuation",
          sch.main(["add", "--in", "2m", "--to", "orchestrator", "--loop", "auto",
                    "--channel", "CID_OM",
                    "--text", "Continue: step 1. Done when: check.py exits 0."]) == 0
          and len(sch.list_loops()) == 1 and sch.list_loops()[0]["max"] == 3
          and sch.load_jobs()[0].get("loop") == sch.list_loops()[0]["id"])
    _lid = sch.list_loops()[0]["id"]
    check("loop: one queued continuation at a time - a loop is a chain, not a fan",
          sch.main(["add", "--in", "2m", "--to", "orchestrator", "--loop", _lid,
                    "--text", "again"]) == 2)

    def _fire_loop_job():
        jobs = sch.load_jobs()
        for j in jobs:
            j["nextRun"] = (D.now() - timedelta(seconds=30)).strftime(sch.FMT)
        sch.save_jobs(jobs)
        wd._last_schedule_check = 0
        wd._last_jobs_written = None
        sent.clear()
        wd.fire_due_schedules()

    _ombox2 = wd.INBOX / "orchestrator"
    for f in (_ombox2.glob("sched-*.json") if _ombox2.is_dir() else []):
        f.unlink()
    _fire_loop_job()
    _fired = sorted(_ombox2.glob("sched-*.json"))
    _fenv = json.loads(_fired[-1].read_text(encoding="utf-8")) if _fired else {}
    check("loop: --channel rides into the fired envelope",
          _fenv.get("channelId") == "CID_OM" and _fenv.get("from") == "schedule")
    check("loop: the fire is counted on the ledger",
          sch.load_loop(_lid).get("fired") == 1)
    check("loop: the fired one-shot leaves the queue (chain, not standing order)",
          not [j for j in sch.load_jobs() if j.get("loop") == _lid])
    # runs 2 and 3 use the budget up
    for _ in range(2):
        sch.main(["add", "--in", "2m", "--to", "orchestrator", "--loop", _lid,
                  "--text", "Continue."])
        _fire_loop_job()
    check("loop: the budget is spent run by run",
          sch.load_loop(_lid).get("fired") == 3)
    check("loop: the add past budget is refused with the checkpoint instruction",
          sch.main(["add", "--in", "2m", "--to", "orchestrator", "--loop", _lid,
                    "--text", "one more"]) == 2)
    # the fire-time belt: a hand-edited job must not sneak past the ledger
    sch.save_jobs([{"id": "handmade", "kind": "at",
                    "at": (D.now() - timedelta(seconds=30)).strftime(sch.FMT),
                    "to": "orchestrator", "text": "sneak", "weekdays": False,
                    "loop": _lid, "channelId": "CID_OM",
                    "nextRun": (D.now() - timedelta(seconds=30)).strftime(sch.FMT)}])
    _before_belt = len(list(_ombox2.glob("sched-*.json")))
    wd._last_schedule_check = 0
    wd._last_jobs_written = None
    sent.clear()
    wd.fire_due_schedules()
    check("loop: fire-time belt - a hand-edited job past budget never fires",
          len(list(_ombox2.glob("sched-*.json"))) == _before_belt
          and not sch.load_jobs())
    check("loop: ...the loop is closed and its channel told once",
          sch.load_loop(_lid).get("closed") == "budget"
          and any("used its budget" in s[1] for s in sent))
    # close: ends the loop and clears its pending continuation
    sch.main(["add", "--in", "2m", "--to", "orchestrator", "--loop", "auto",
              "--text", "Continue: other work."])
    _lid2 = [led["id"] for led in sch.list_loops() if not led.get("closed")][0]
    check("loop: close ends the loop and clears its queued job",
          sch.main(["loop", "close", _lid2]) == 0
          and sch.load_loop(_lid2).get("closed") == "done"
          and not [j for j in sch.load_jobs() if j.get("loop") == _lid2])
    check("loop: list is readable and exits clean",
          sch.main(["loop", "list"]) == 0)
    sch.save_jobs([])
    for f in _ombox2.glob("sched-*.json"):
        f.unlink()
    sch.ROOT, sch.loop_budget_default = _real_sched_root, _real_lbd
    sch.own_session = _real_own
    sent.clear()

    # the alarm: once per incident, not once per further miss
    posts = []
    _rsm, _rbc = api.send_message, wd.broadcast_channel_id
    api.send_message = lambda cid, text, files=None: posts.append((cid, text))
    wd.broadcast_channel_id = lambda name="alerts": "C_ALERTS"
    try:
        wd._missed_alerted.clear()
        _stale(missed=wd.MISSED_ALARM_AT - 2)          # -> 2, still under
        check("no alarm below the threshold", posts == [])
        _stale(missed=wd.MISSED_ALARM_AT - 1)          # -> 3, crosses
        check("crossing 3 misses posts one alert", len(posts) == 1)
        check("the alert goes to #alerts", posts and posts[0][0] == "C_ALERTS")
        check("the alert names the routine", posts and "j2" in posts[0][1])
        check("the alert is not a markdown table", posts and "|" not in posts[0][1])
        _stale(missed=9)
        check("a climbing counter does not re-page every tick", len(posts) == 1)
        wd._missed_alerted.clear()
        _stale(missed=0)                               # -> 1, incident over
        check("dropping back under the threshold re-arms it",
              "j2" not in wd._missed_alerted)
    finally:
        api.send_message, wd.broadcast_channel_id = _rsm, _rbc

    # the real lookup, against a controlled map
    _rbm5 = wd.build_map
    wd.build_map = lambda schema: {
        "C_X": type("T", (), {"channel_name": "app"})(),
        "C9": type("T", (), {"channel_name": "alerts"})()}
    try:
        check("broadcast_channel_id finds a session-less channel by name",
              wd.broadcast_channel_id("alerts") == "C9")
        check("and returns None rather than raising when it is absent",
              wd.broadcast_channel_id("nope") is None)
        wd.build_map = lambda schema: (_ for _ in ()).throw(RuntimeError("boom"))
        check("a broken map cannot take down the fire loop",
              wd.broadcast_channel_id("alerts") is None)
    finally:
        wd.build_map = _rbm5

    # == /omnius teaches routines ============================================
    # A desk that cannot create one means the whole feature is CLI-only, and a
    # routine created WITHOUT the silence condition is worse than none: nine
    # "nothing new" posts a day train him to ignore the channel.
    _sk = (HERE.parent.parent / ".claude" / "skills" / "omnius" / "SKILL.md").read_text(encoding="utf-8")
    check("/omnius tells a desk how to create a routine", "schedule.py add" in _sk)
    check("...including the window and weekday flags",
          "--between" in _sk and "--weekdays" in _sk)
    check("...and to write the SILENCE condition into the envelope",
          "silently" in _sk and "no outbox file" in _sk)
    check("...and to echo the fire times back, so a misparse is caught at once",
          "next three fire times" in _sk or "three fire times" in _sk)
    check("...and to confirm once for a routine that acts outward",
          "outward" in _sk)
    check("...and points management at !cron rather than the desk",
          "!cron" in _sk)
    check("routines are documented as living in config/, so they travel",
          "config\\routines.json" in _sk)

    # The seed must land at <leaf>/memory INSIDE the archive. Handing it to tar
    # cannot work: --exclude=<leaf>/memory drops the staged copy too (release
    # shipped with NO memory), and naming the stage "memory" to dodge that put
    # it at the archive ROOT (unzip gave omnius\ + an orphan memory\). Both
    # builds reported success - only extracting showed it, 2026-08-09.
    check("the seed is INJECTED into the zip, not handed to tar",
          "CreateEntryFromFile" in _pack and '"$leaf/$rel"' in _pack)
    check("...and tar is no longer given the seed as a second -C source",
          "'-C', $freshStage" not in _pack,
          "that path is the one --exclude silently eats")
    check("...and the injection runs BEFORE the audit, so the seed is scanned too",
          _pack.index("CreateEntryFromFile") < _pack.index("release_sanitize.py"))

    check("schema really declares an #alerts channel",
          any(c.get("name") == "alerts"
              for cat in real_schema.get("initial", {}).get("categories", [])
              for c in cat.get("channels", [])))
    sch.save_jobs([])

    # == embeds ===============================================================
    # Four documents advertised embeds and the pinned #fleet-status board
    # depends on them; no line of code implemented one until now.
    print("== embeds ==")
    calls = []
    real_api = api.api
    api.api = lambda m, p, body=None, params=None, files=None: (
        calls.append((m, p, body, files)) or {"id": "m1"})
    try:
        api.send_embed("C1", "Fleet", "all good", fields=[("sessions", "2", True)],
                       footer="HomeAsus")
        m, p, body, files = calls[-1]
        e = body["embeds"][0]
        check("embed: posts to the channel", m == "POST" and p.endswith("/messages"))
        check("embed: carries title/description/footer",
              e["title"] == "Fleet" and e["description"] == "all good"
              and e["footer"]["text"] == "HomeAsus")
        check("embed: fields shaped for Discord",
              e["fields"][0]["name"] == "sessions" and e["fields"][0]["value"] == "2")
        api.send_embed("C1", "T", "d", message_id="m9")
        m, p, _, _ = calls[-1]
        check("embed: message_id edits instead of posting",
              m == "PATCH" and p.endswith("/messages/m9"))
        api.send_embed("C1", "T", "secret " + api.TOKEN)
        check("embed: description redacted", api.TOKEN not in calls[-1][2]["embeds"][0]["description"])
        thumb = SAND / "logo.png"; thumb.write_bytes(b"\x89PNG x")
        api.send_embed("C1", "T", thumbnail=str(thumb))
        _, _, body, files = calls[-1]
        check("embed: local thumbnail uploaded as attachment://",
              body["embeds"][0]["thumbnail"]["url"] == "attachment://logo.png"
              and files == [str(thumb)])
        api.send_embed("C1", "T", fields=[(f"n{i}", "v", False) for i in range(40)])
        check("embed: field list capped at Discord's 25",
              len(calls[-1][2]["embeds"][0]["fields"]) == 25)
    finally:
        api.api = real_api

    # == bus transcript =======================================================
    # Envelopes and outbox files are deleted once handled, so without this the
    # only copy of a remote conversation lived in Discord.
    print("== bus transcript ==")
    # A session of its own: handle_message tests above already transcribe into
    # demo-app.*, so asserting exact counts there would be order-dependent.
    TS = "ttest.app"
    wd.transcribe(TS, "in", "please add vitest", channel="app", channel_id="c1")
    wd.transcribe(TS, "out", "added it", channel="app", channel_id="c1",
                  files=[str(SAND / "shot.png")])
    tfiles = list((wd.TRANSCRIPTS / TS).glob("*.jsonl"))
    check("transcript: monthly file created", len(tfiles) == 1)
    rows = [json.loads(x) for x in tfiles[0].read_text(encoding="utf-8").splitlines() if x.strip()]
    check("transcript: both directions appended", len(rows) == 2)
    check("transcript: inbound recorded", rows[0]["dir"] == "in" and "vitest" in rows[0]["text"])
    check("transcript: attachments recorded by name", rows[1]["files"] == ["shot.png"])
    check("transcript: channel id carried", rows[0]["channelId"] == "c1")
    # a token pasted into chat must not become a new secret sink on disk
    wd.transcribe(TS, "in", f"my token is {api.TOKEN}", channel="app")
    rows = [json.loads(x) for x in tfiles[0].read_text(encoding="utf-8").splitlines() if x.strip()]
    check("transcript: secrets redacted on write", api.TOKEN not in rows[-1]["text"])

    import transcript as tr
    tr.TRANSCRIPTS = wd.TRANSCRIPTS
    found = [e for _, e in tr.entries(session=TS)]
    check("transcript search: reads back what was written", len(found) == 3)
    check("transcript search: survives a torn last line", True)
    (wd.TRANSCRIPTS / TS / "torn.jsonl").write_text(
        '{"ts":"x","text":"good"}\n{"broken', encoding="utf-8")
    # Live-only bug, found by running it: Discord text is full of emoji and the
    # Windows console is cp1252. watchdog.log() already learned this once; the
    # CLI reintroduced it. Reproduce through a real subprocess with a cp1252
    # stdout - an in-process check would not catch it.
    import subprocess as _sp
    emo = SAND / "emoji-probe"
    (emo / "s1").mkdir(parents=True, exist_ok=True)
    (emo / "s1" / "2026-07.jsonl").write_text(
        json.dumps({"ts": now(), "dir": "out", "channel": "c", "text": "🔎 ok", "files": []}) + "\n",
        encoding="utf-8")
    probe = _sp.run(
        [sys.executable, "-c",
         "import sys;sys.path.insert(0,r'%s');import transcript as t;"
         "t.TRANSCRIPTS=__import__('pathlib').Path(r'%s');sys.exit(t.main(['tail']))"
         % (HERE, emo)],
        capture_output=True, env={**_os.environ, "PYTHONIOENCODING": "cp1252"})
    check("transcript CLI: emoji on a cp1252 console does not crash",
          probe.returncode == 0, probe.stderr.decode("utf-8", "replace")[-160:])

    check("transcript search: torn line skipped, scan continues",
          len([e for _, e in tr.entries(session=TS)]) == 4)

    # == session-notes staleness ==============================================
    # Siblings coordinate through memory\sessions\<component>.md; nothing ever
    # checked whether it was current, so "keep yours current" was aspirational.
    print("== session notes ==")
    wd.ROOT = SAND
    check("notes_age: orchestrator has no component notes", wd.notes_age("orchestrator") == "")
    check("notes_age: missing notes are flagged", "no session notes" in wd.notes_age("demo-app.app"))
    nd = SAND / "projects" / "demo-app" / "memory" / "sessions"
    nd.mkdir(parents=True, exist_ok=True)
    fresh = nd / "app.md"
    fresh.write_text("notes", encoding="utf-8")
    check("notes_age: fresh notes are quiet", wd.notes_age("demo-app.app") == "")
    # Nine days AND A MINUTE. notes_age int()s the day count, so an age landing
    # a hair under 9.0 - which utime rounding and filesystem timestamp
    # resolution can produce - reports "8d old" and fails a test that is not
    # actually about rounding. Seen failing once, then passing twice in a row
    # (2026-08-15); a suite that cries wolf gets ignored.
    old = _os.path.getmtime(fresh) - (9 * 86400 + 60)
    _os.utime(fresh, (old, old))
    check("notes_age: 9-day-old notes are flagged", "9d old" in wd.notes_age("demo-app.app"))

    # == outbox routing ========================================================
    # Two projects each owning an "#app" is the normal case, not an edge case:
    # name-based routing silently posted into whichever came first in dict order.
    print("== outbox routing ==")
    two = {
        "id-alpha-app": T("app", "alpha.app", "📁 alpha"),
        "id-beta-app":  T("app", "beta.app", "📁 beta"),
        "id-alerts":    T("alerts", None, "🎛 ORCHESTRATOR"),
    }
    R = wd.resolve_outbox_target
    check("routing: channelId wins over an ambiguous name",
          R(two, "beta.app", {"channelId": "id-beta-app", "channel": "app"}) == "id-beta-app")
    check("routing: name resolves to the sender's own channel, not the first match",
          R(two, "beta.app", {"channel": "app"}) == "id-beta-app")
    check("routing: sibling project's channel is refused",
          R(two, "alpha.app", {"channelId": "id-beta-app"}) == wd.REFUSED)
    check("routing: cross-project post by name is refused, not misdelivered",
          R(two, "alpha.app", {"channel": "app"}) == "id-alpha-app")
    check("routing: any session may post to a broadcast channel",
          R(two, "alpha.app", {"channelId": "id-alerts"}) == "id-alerts")
    check("routing: unknown channelId falls through to the name",
          R(two, "alpha.app", {"channelId": "gone", "channel": "app"}) == "id-alpha-app")
    # 2026-08-04: permission asks were pinned to #alerts, so answering one meant
    # leaving the channel he was working in - three times for one scaffold.
    # "I have to jump from omnius to alerts - not practical."
    check("routing: no channel key posts in the desk's OWN channel",
          R(two, "beta.app", {"text": "?"}) == "id-beta-app")
    check("routing: fallback is used only when the desk has no channel",
          R(two, "beta.app", {"fallback": "alerts"}) == "id-beta-app"
          and R(two, "ghost.desk", {"fallback": "alerts"}) == "id-alerts")
    check("routing: a deskless envelope with no fallback is still kept, not guessed",
          R(two, "ghost.desk", {"text": "?"}) is None)
    _relay_src = (_rr / "tools" / "discord" / "permission_relay.py").read_text(encoding="utf-8")
    check("the ask itself no longer pins itself to #alerts",
          '"channel": "alerts"' not in _relay_src
          and _relay_src.count('"fallback": "alerts"') == 2)

    # (The boot-grace spawn lease died with terminal spawns: a run's lease now
    # carries the child PID from birth, so adoption/expiry is pid liveness -
    # covered in "one run per desk" above.)

    # == desk mail (docs\DELEGATION.md D1-D4) ==================================
    # A desk delegates with an outbox `to` file; the watchdog routes it. Driven
    # both directly and through flush_outboxes, against real sandbox folders -
    # the registry IS the filesystem, so the folders are the fixture.
    print("== desk mail: envelope v2 ==")
    for _comp in ("app", "web", "api"):
        (SAND / "projects" / "alpha" / _comp).mkdir(parents=True, exist_ok=True)
    (SAND / "projects" / "beta" / "app").mkdir(parents=True, exist_ok=True)
    dm = {
        "id-alpha-app": T("app", "alpha.app", "📁 alpha"),
        "id-alpha-web": T("web", "alpha.web", "📁 alpha"),
        "id-alpha-api": T("api", "alpha.api", "📁 alpha"),
        "id-beta-app":  T("app", "beta.app", "📁 beta"),
        "id-alerts":    T("alerts", None, "🎛 ORCHESTRATOR"),
    }
    _real_ttl, _real_gatereq = wd._hop_ttl, wd._gate_required
    wd._hop_ttl = lambda: 2          # deterministic: never read this machine's config
    wd._gate_required = lambda: True
    wd._desk_id_cache.clear()
    wd.PERMS.mkdir(parents=True, exist_ok=True)
    for _p in wd.PERMS.glob("*.json"):
        _p.unlink()

    def dmail(sender, body, stem):
        box = wd.OUTBOX / sender
        box.mkdir(parents=True, exist_ok=True)
        p = box / f"{stem}.json"
        p.write_text(json.dumps(body), encoding="utf-8")
        return p

    sent.clear(); spawned.clear()
    p1 = dmail("alpha.app", {"to": "alpha.web", "text": "finding 3: fix the api",
                             "origin": {"channelId": "id-alpha-app", "from": "owner"}},
               "1700000000001")
    r1 = wd.deliver_desk_mail(dm, "alpha.app", p1, json.loads(p1.read_text(encoding="utf-8")))
    _webbox = sorted((wd.INBOX / "alpha.web").glob("*.json"))
    env1 = json.loads(_webbox[0].read_text(encoding="utf-8")) if _webbox else {}
    check("desk mail: a 'to' outbox file becomes a v2 inbox envelope",
          r1 == "delivered" and env1.get("kind") == "desk" and env1.get("from") == "alpha.app"
          and env1.get("id") == "dm-alpha.app-1700000000001",
          f"r={r1} env={env1.get('id')}")
    check("desk mail: the outbox file is consumed on delivery", not p1.exists())
    check("desk mail: a fresh chain gets hop_ttl from config (2 -> 1 after one hop)",
          env1.get("hops") == 1 and str(env1.get("thread", "")).startswith("t-"))
    check("desk mail: origin travels with the chain, stamped with its starter",
          (env1.get("origin") or {}).get("channelId") == "id-alpha-app"
          and (env1.get("origin") or {}).get("session") == "alpha.app")
    check("desk mail: the inbox write is atomic - no .part/.tmp litter",
          not list((wd.INBOX / "alpha.web").glob("*.tmp"))
          and not list((wd.INBOX / "alpha.web").glob("*.part")))
    check("desk mail: the visible copy posts in the RECIPIENT's channel",
          bool(sent) and sent[-1][0] == "id-alpha-web" and "[desk mail]" in sent[-1][1])
    check("desk mail: delivery stamps the sender's .last-posted (silence announcer)",
          (wd.OUTBOX / "alpha.app" / ".last-posted").exists())
    check("desk mail: delivery kicks ensure_runner on the target", "alpha.web" in spawned)

    def _transcript_tail(sess):
        d = wd.TRANSCRIPTS / sess
        fs = sorted(d.glob("*.jsonl")) if d.is_dir() else []
        return fs[-1].read_text(encoding="utf-8") if fs else ""
    check("desk mail: both halves reach the bus transcript",
          "finding 3" in _transcript_tail("alpha.app")
          and "finding 3" in _transcript_tail("alpha.web"))

    print("== desk mail routing ==")
    # the today-untested flush refusal first: a foreign-channel envelope
    (wd.OUTBOX / "alpha.app" / "1700000000010.json").write_text(
        json.dumps({"text": "x", "channelId": "id-beta-app"}), encoding="utf-8")
    wd.flush_outboxes(dm)
    check("flush: a foreign-channel envelope is renamed .refused, never misdelivered",
          (wd.OUTBOX / "alpha.app" / "1700000000010.refused").exists())
    sent.clear()
    p_bad = dmail("alpha.app", {"to": "Not A Desk!", "text": "x"}, "1700000000002")
    check("desk-target: a malformed id is refused and renamed .refused",
          wd.deliver_desk_mail(dm, "alpha.app", p_bad,
                               {"to": "Not A Desk!", "text": "x"}) == "refused"
          and (wd.OUTBOX / "alpha.app" / "1700000000002.refused").exists())
    check("desk-target: refusal tells the sender's channel in one line",
          bool(sent) and sent[-1][0] == "id-alpha-app" and "could not deliver" in sent[-1][1])
    p_res = dmail("alpha.app", {"to": "omnius", "text": "x"}, "1700000000003")
    check("desk-target: a reserved sender name as target is refused",
          wd.deliver_desk_mail(dm, "alpha.app", p_res,
                               {"to": "omnius", "text": "x"}) == "refused")
    p_ghost = dmail("alpha.app", {"to": "ghost.desk", "text": "x"}, "1700000000004")
    check("desk-target: a desk with no folder is refused - no phantom inbox is created",
          wd.deliver_desk_mail(dm, "alpha.app", p_ghost,
                               {"to": "ghost.desk", "text": "x"}) == "refused"
          and not (wd.INBOX / "ghost.desk").exists())
    p_self = dmail("alpha.app", {"to": "alpha.app", "text": "x"}, "1700000000005")
    check("desk-target: self-address is refused - continuation is the schedule's job",
          wd.deliver_desk_mail(dm, "alpha.app", p_self,
                               {"to": "alpha.app", "text": "x"}) == "refused"
          and "schedule" in sent[-1][1])

    print("== hops and threads ==")
    t1 = env1.get("thread")
    # a threadless reply is glued to the chain that last delivered to the sender
    p2 = dmail("alpha.web", {"to": "alpha.app", "text": "done, verify me"}, "1700000000011")
    r2 = wd.deliver_desk_mail(dm, "alpha.web", p2, json.loads(p2.read_text(encoding="utf-8")))
    _appbox = sorted((wd.INBOX / "alpha.app").glob("*.json"))
    env2 = json.loads(_appbox[-1].read_text(encoding="utf-8")) if _appbox else {}
    check("threads: a threadless reply is glued to the thread that woke the sender",
          r2 == "delivered" and env2.get("thread") == t1)
    check("hops: a reply along a recorded edge is free",
          env2.get("hops") == 1, f"hops={env2.get('hops')}")
    # dedupe: the crash window between inbox-write and outbox-unlink (while the
    # chain is still open - a closed chain refuses before dedupe is consulted)
    p_dup = dmail("alpha.app", {"to": "alpha.web", "text": "finding 3: fix the api",
                                "thread": t1}, "1700000000001")
    _web_before = len(list((wd.INBOX / "alpha.web").glob("*.json")))
    check("threads: redelivery after a crash is a no-op, never a duplicate",
          wd.deliver_desk_mail(dm, "alpha.app", p_dup,
                               json.loads(p_dup.read_text(encoding="utf-8"))) == "duplicate"
          and not p_dup.exists()
          and len(list((wd.INBOX / "alpha.web").glob("*.json"))) == _web_before)
    # a new edge through the FLUSH branch spends the last hop
    dmail("alpha.app", {"to": "alpha.api", "text": "and the api half", "thread": t1},
          "1700000000012")
    wd.flush_outboxes(dm)
    _apibox = sorted((wd.INBOX / "alpha.api").glob("*.json"))
    env3 = json.loads(_apibox[-1].read_text(encoding="utf-8")) if _apibox else {}
    check("hops: each forward hop decrements the thread ledger (flush branch)",
          env3.get("id") == "dm-alpha.app-1700000000012" and env3.get("hops") == 0)
    # a reply is still free at zero hops - unwinding is never starved
    p4a = dmail("alpha.web", {"to": "alpha.app", "text": "still unwinding", "thread": t1},
                "1700000000013")
    check("hops: a reply is free even at zero hops left",
          wd.deliver_desk_mail(dm, "alpha.web", p4a,
                               json.loads(p4a.read_text(encoding="utf-8"))) == "delivered")
    # ...but a NEW edge at zero closes the chain and checkpoints the human
    sent.clear()
    p4b = dmail("alpha.api", {"to": "alpha.web", "text": "one more", "thread": t1},
                "1700000000014")
    r4b = wd.deliver_desk_mail(dm, "alpha.api", p4b, json.loads(p4b.read_text(encoding="utf-8")))
    led1 = wd._load_thread(t1)
    check("hops: an exhausted chain is refused and closed",
          r4b == "refused" and led1 and led1.get("closed") == "hops")
    check("hops: the owner sees a checkpoint in the origin channel",
          any("hop limit" in s[1] and s[0] == "id-alpha-app" for s in sent))
    # closed chains refuse further mail by id
    p4c = dmail("alpha.app", {"to": "alpha.web", "text": "zombie", "thread": t1},
                "1700000000015")
    check("threads: a closed chain refuses further mail",
          wd.deliver_desk_mail(dm, "alpha.app", p4c,
                               json.loads(p4c.read_text(encoding="utf-8"))) == "refused")
    check("threads: the ledger is a FILE and survives a restart",
          wd._thread_path(t1).exists() and wd._load_thread(t1).get("closed") == "hops")
    # the storm backstop: hop-free replies cannot ping-pong forever
    wd._hop_ttl = lambda: 1          # cap = 4 deliveries
    ps1 = dmail("alpha.app", {"to": "alpha.web", "text": "storm seed"}, "1700000000030")
    wd.deliver_desk_mail(dm, "alpha.app", ps1, json.loads(ps1.read_text(encoding="utf-8")))
    t2 = json.loads(sorted((wd.INBOX / "alpha.web").glob("*.json"))[-1]
                    .read_text(encoding="utf-8")).get("thread")
    _storm = None
    for i in range(4):
        psn = dmail("alpha.web", {"to": "alpha.app", "text": f"pong {i}", "thread": t2},
                    f"170000000004{i}")
        _storm = wd.deliver_desk_mail(dm, "alpha.web", psn,
                                      json.loads(psn.read_text(encoding="utf-8")))
    check("threads: the deliveries backstop stops a ping-pong storm",
          _storm == "refused" and wd._load_thread(t2).get("closed") == "storm",
          f"last={_storm}")
    wd._hop_ttl = lambda: 2
    # The reviewer's invariant, pinned by name: replies are DIRECTIONAL. A
    # repeated forward along an already-recorded edge is NOT a reply - only a
    # true reversal travels free; same-direction traffic always spends budget,
    # so "replies are free" can never become free forwarding.
    pf1 = dmail("alpha.app", {"to": "alpha.web", "text": "fwd once"}, "1700000000050")
    wd.deliver_desk_mail(dm, "alpha.app", pf1, json.loads(pf1.read_text(encoding="utf-8")))
    t3 = json.loads(sorted((wd.INBOX / "alpha.web").glob("*.json"))[-1]
                    .read_text(encoding="utf-8")).get("thread")
    pf2 = dmail("alpha.app", {"to": "alpha.web", "text": "fwd again", "thread": t3},
                "1700000000051")
    r_f2 = wd.deliver_desk_mail(dm, "alpha.app", pf2,
                                json.loads(pf2.read_text(encoding="utf-8")))
    env_f2 = json.loads(sorted((wd.INBOX / "alpha.web").glob("*.json"))[-1]
                        .read_text(encoding="utf-8"))
    check("hops: a repeated forward along an existing edge is NOT a reply - it still costs",
          r_f2 == "delivered" and env_f2.get("hops") == 0
          and wd._load_thread(t3).get("hopsLeft") == 0,
          f"r={r_f2} hops={env_f2.get('hops')}")

    print("== fleet senders (desk mail classification) ==")
    check("a desk id in 'from' is not a person", wd.is_human_sender("alpha.web") is False)
    check("a '-job' sender is fleet mail too (transcribe-job predates this)",
          wd.is_human_sender("transcribe-job") is False)
    check("owner, guests and strangers still count as people",
          wd.is_human_sender("owner") is True and wd.is_human_sender("guestina") is True
          and wd.is_human_sender("someone new") is True)
    check("a box holding only desk mail opens no window",
          wd.human_mail_waiting("alpha.web") is False)
    check("desk mail never trips the deaf-desk pager",
          wd.oldest_human_envelope("alpha.web") == (None, 0.0))
    # guest labels may never look like the fleet (config side, fails closed)
    import omnius_config as _dcfg                                     # noqa: E402
    _dreal = _dcfg.load
    _dfake = {
        "guest.alpha.web": {"user_id": "424242424242424250", "channels": "c1"},
        "guest.daybook": {"user_id": "424242424242424251", "channels": "c1"},
        "guest.render-job": {"user_id": "424242424242424252", "channels": "c1"},
        "guest.plain": {"user_id": "424242424242424253", "channels": "c1"},
    }
    try:
        _dcfg.load = lambda name, legacy=None: (
            _dfake if name == "guests" else _dreal(name, legacy))
        _dread = _dcfg.guests()
        check("guests: a label with a dot is refused at config load",
              "alpha.web" not in _dread)
        check("guests: a dotless desk name is refused at config load",
              "daybook" not in _dread)
        check("guests: a -job label is refused at config load",
              "render-job" not in _dread and "plain" in _dread)
        check("guests: the desk-shaped rejections are reported, not swallowed",
              sum(1 for p in _dcfg.problems() if "desk id or" in p) >= 3)
    finally:
        _dcfg.load = _dreal

    print("== cross-project gate ==")
    F = wd.free_pair
    check("gate: same-project mail passes free", F("alpha.app", "alpha.web") is True)
    check("gate: orchestrator mail is never gated", F("orchestrator", "beta.app") is True)
    check("gate: cross-project, orchestrator, tool and daybook targets all hold",
          F("alpha.app", "beta.app") is False and F("alpha.app", "orchestrator") is False
          and F("tool.email", "tool.whisper") is False and F("alpha.app", "daybook") is False)
    sent.clear()
    pg = dmail("alpha.app", {"to": "beta.app", "text": "cross the border"}, "1700000000020")
    rg = wd.deliver_desk_mail(dm, "alpha.app", pg, json.loads(pg.read_text(encoding="utf-8")))
    _gates = wd.pending_gates()
    check("gate: cross-project mail is HELD, not delivered",
          rg == "held" and len(_gates) == 1 and not pg.exists()
          and not list((wd.INBOX / "beta.app").glob("*.json")))
    check("gate: the ask names both desks and carries a code",
          bool(sent) and "cross-project" in sent[-1][1] and _gates[0]["code"] in sent[-1][1])
    spawned.clear()
    _ans = wd.answer_gate("ok", dm)
    check("gate: 'ok' delivers the held envelope",
          bool(_ans) and "delivered" in _ans and not wd.pending_gates()
          and len(list((wd.INBOX / "beta.app").glob("*.json"))) == 1
          and "beta.app" in spawned)
    pg2 = dmail("alpha.app", {"to": "beta.app", "text": "second try"}, "1700000000021")
    wd.deliver_desk_mail(dm, "alpha.app", pg2, json.loads(pg2.read_text(encoding="utf-8")))
    _code2 = wd.pending_gates()[0]["code"]
    _ans2 = wd.answer_gate(f"no {_code2}", dm)
    check("gate: 'no' drops it and says so",
          bool(_ans2) and "not delivered" in _ans2 and not wd.pending_gates()
          and bool(list(wd.GATE.glob("*.refused"))))
    pg3 = dmail("alpha.app", {"to": "beta.app", "text": "third"}, "1700000000022")
    wd.deliver_desk_mail(dm, "alpha.app", pg3, json.loads(pg3.read_text(encoding="utf-8")))
    (wd.PERMS / "permreq1.json").write_text(json.dumps(
        {"id": "permreq1", "code": "ppp111", "session": "alpha.app", "tool": "Bash"}),
        encoding="utf-8")
    check("gate: a bare 'ok' never answers a gate while a permission ask is pending",
          wd.answer_gate("ok", dm) is None and len(wd.pending_gates()) == 1)
    (wd.PERMS / "permreq1.json").unlink()
    _g3 = wd.pending_gates()[0]
    _g3f = wd.GATE / f"{_g3['id']}.json"
    _g3rec = json.loads(_g3f.read_text(encoding="utf-8"))
    _g3rec["askedTs"] = time.time() - 7200
    _g3f.write_text(json.dumps(_g3rec), encoding="utf-8")
    sent.clear()
    wd.sweep_gates(dm)
    check("gate: silence past the deadline refuses - fail closed",
          not wd.pending_gates() and any("no answer within" in s[1] for s in sent))
    pg4 = dmail("alpha.app", {"to": "beta.app", "text": "fourth"}, "1700000000023")
    wd.deliver_desk_mail(dm, "alpha.app", pg4, json.loads(pg4.read_text(encoding="utf-8")))
    _g4 = wd.pending_gates()[0]
    _g4f = wd.GATE / f"{_g4['id']}.json"
    _g4rec = json.loads(_g4f.read_text(encoding="utf-8"))
    _g4rec["lastAskTs"] = 0.0        # "asked before this boot"
    _g4f.write_text(json.dumps(_g4rec), encoding="utf-8")
    sent.clear()
    wd.sweep_gates(dm)
    check("gate: a pending ask survives a restart - re-asked once, same code, old deadline",
          any(_g4["code"] in s[1] for s in sent)
          and wd.pending_gates()[0]["code"] == _g4["code"]
          and json.loads(_g4f.read_text(encoding="utf-8"))["askedTs"] == _g4rec["askedTs"])
    sent.clear()
    wd.sweep_gates(dm)
    check("gate: ...and only once per boot, not per tick",
          not any(_g4["code"] in s[1] for s in sent))
    _r5 = wd.handle_message(msg("999999999999999999", f"ok {_g4['code']}"), "CID_OM",
                            T("omnius", "orchestrator"), me, dm)
    check("gate: the owner's answer is consumed at dispatch, never delivered as mail",
          _r5 == "gate" and not wd.pending_gates())
    check("config: the [delegation] keys are in SPEC and visible to !config",
          any(s == "delegation" and k == "hop_ttl" for _, s, k, _e, _d, _k2 in _dcfg.SPEC)
          and any(s == "delegation" and k == "cross_project_requires_ok"
                  for _, s, k, _e, _d, _k2 in _dcfg.SPEC)
          and any(s == "delegation" and k == "loop_budget"
                  for _, s, k, _e, _d, _k2 in _dcfg.SPEC))
    # restore the config-backed readers and tidy shared fixtures
    wd._hop_ttl, wd._gate_required = _real_ttl, _real_gatereq
    wd._desk_id_cache.clear()
    sent.clear(); spawned.clear()

    # == slash pass-through (docs\DELEGATION.md D6) ============================
    print("== slash pass-through ==")
    _saved_slash = wd.SLASH_SKILLS
    _slashbox = wd.INBOX / "demo-app.app"

    def _slash_env():
        fs = sorted(_slashbox.glob("*.json")) if _slashbox.is_dir() else []
        return json.loads(fs[-1].read_text(encoding="utf-8")) if fs else {}

    def _clean_slashbox():
        if _slashbox.is_dir():
            for f in _slashbox.glob("*.json"):
                f.unlink()

    wd.SLASH_SKILLS = {"status"}
    _clean_slashbox(); sent.clear()
    r = wd.handle_message(msg("999999999999999999", "/status all desks please"),
                          "CID_APP", T("app", "demo-app.app"), me, {})
    _e = _slash_env()
    check("slash: an allow-listed /skill stamps the envelope, text verbatim",
          r in ("spawned", "queued", "delivered") and _e.get("slash") == "status"
          and _e.get("text") == "/status all desks please")
    _clean_slashbox(); sent.clear()
    r = wd.handle_message(msg("999999999999999999", "/deploy prod"),
                          "CID_APP", T("app", "demo-app.app"), me, {})
    check("slash: an unlisted /skill is refused in-channel and nothing is delivered",
          r == "slash-refused" and not list(_slashbox.glob("*.json"))
          and bool(sent) and "pass-through" in sent[-1][1])
    _clean_slashbox()
    r = wd.handle_message(msg("999999999999999999", "/omnius"),
                          "CID_APP", T("app", "demo-app.app"), me, {})
    check("slash: /omnius is a no-op alias for plain mail - delivered, no stamp",
          r in ("spawned", "queued", "delivered")
          and list(_slashbox.glob("*.json")) and "slash" not in _slash_env())
    _saved_g2 = dict(wd.GUESTS)
    wd.GUESTS = {"guestina": {"id": "424242424242424260", "channels": ["CID_APP"],
                              "name": "Guestina", "scope": ""}}
    _clean_slashbox()
    r = wd.handle_message(msg("424242424242424260", "/status please"),
                          "CID_APP", T("app", "demo-app.app"), me, {})
    check("slash: a guest's slash is plain text - never passed through",
          r in ("spawned", "queued", "delivered")
          and list(_slashbox.glob("*.json")) and "slash" not in _slash_env())
    wd.GUESTS = _saved_g2
    wd.SLASH_SKILLS = set()
    _clean_slashbox()
    r = wd.handle_message(msg("999999999999999999", "/status now"),
                          "CID_APP", T("app", "demo-app.app"), me, {})
    check("slash: an empty allow-list passes nothing - closed by default",
          r == "slash-refused" and not list(_slashbox.glob("*.json")))
    # the config reader itself, patched like the guest loader above
    _sreal = _dcfg.load
    try:
        _dcfg.load = lambda name, legacy=None: (
            {"skills": {"allowed": "status, /watch, bad*name"}}
            if name == "skills" else _sreal(name, legacy))
        _sread = _dcfg.slash_skills()
        check("slash config: names are validated, slashes stripped, junk skipped",
              _sread == {"status", "watch"})
        check("slash config: the junk label is reported, not swallowed",
              any("skills.ini" in p for p in _dcfg.problems()))
        _dcfg.load = lambda name, legacy=None: (
            {} if name == "skills" else _sreal(name, legacy))
        check("slash config: no file means nothing passes",
              _dcfg.slash_skills() == set())
    finally:
        _dcfg.load = _sreal
    wd.SLASH_SKILLS = _saved_slash
    _clean_slashbox(); sent.clear()

    # == !update (self-update from the repo) ===================================
    # One verb carries the whole story: preview, ff-only pull, suite gate with
    # rollback, restamp, reload. Git and the suite are stubbed - these prove
    # the DECISIONS, and the two source checks at the end pin the build side.
    print("== !update ==")
    _real_git, _real_us, _real_dr = wd._git, wd._update_suite, wd.do_reload
    _real_restamp = wd._update_restamp
    wd._update_restamp = lambda: None
    _reloaded = []
    wd.do_reload = lambda cid, announce=True: _reloaded.append(cid)
    _upd_calls = []

    def _script_git(script):
        def fake(*args, timeout=60):
            _upd_calls.append(args)
            for key, resp in script:
                if args[:len(key)] == key:
                    return resp
            return (0, "")
        return fake

    check("!update is a control verb", "!update" in wd.CONTROL_COMMANDS)
    sent.clear(); _upd_calls[:] = []
    wd._git = _script_git([(("rev-parse", "--is-inside-work-tree"),
                            (1, "fatal: not a git repository"))])
    wd.handle_update("!update", "CID_OM")
    check("update: an unattached install is told how to attach, not left confused",
          bool(sent) and "install.bat" in sent[-1][1])
    sent.clear()
    wd._git = _script_git([
        (("rev-parse", "--is-inside-work-tree"), (0, "true")),
        (("fetch",), (0, "")),
        (("rev-parse", "--short", "HEAD"), (0, "aaa1111\n")),
        (("rev-list",), (0, "0\n")),
    ])
    wd.handle_update("!update", "CID_OM")
    check("update: a current tree says so and stops", "already current" in sent[-1][1])
    sent.clear(); _upd_calls[:] = []
    wd._git = _script_git([
        (("rev-parse", "--is-inside-work-tree"), (0, "true")),
        (("fetch",), (0, "")),
        (("rev-parse", "--short", "HEAD"), (0, "aaa1111\n")),
        (("rev-list",), (0, "3\n")),
        (("log",), (0, "bbb2222 fix a\nccc3333 fix b\nddd4444 fix c\n")),
    ])
    wd.handle_update("!update", "CID_OM")
    check("update: behind -> a preview names the commits and the go verb",
          "3 commit(s) behind" in sent[-1][1] and "!update go" in sent[-1][1]
          and not any(c[:1] == ("pull",) for c in _upd_calls))
    sent.clear(); _upd_calls[:] = []
    wd._git = _script_git([
        (("rev-parse", "--is-inside-work-tree"), (0, "true")),
        (("fetch",), (0, "")),
        (("rev-parse", "--short", "HEAD"), (0, "aaa1111\n")),
        (("rev-list",), (0, "3\n")),
        (("status", "--porcelain"), (0, " M tools/discord/watchdog.py\n")),
    ])
    wd.handle_update("!update go", "CID_OM")
    check("update go: a dirty tree refuses - a pull must never eat local work",
          "not updating" in sent[-1][1]
          and not any(c[:1] == ("pull",) for c in _upd_calls))
    _seq = {"n": 0}

    def _upd_happy(*args, timeout=60):
        _upd_calls.append(args)
        if args[:2] == ("rev-parse", "--is-inside-work-tree"):
            return (0, "true")
        if args[:3] == ("rev-parse", "--short", "HEAD"):
            _seq["n"] += 1
            return (0, "aaa1111\n" if _seq["n"] == 1 else "eee5555\n")
        if args[:1] == ("rev-list",):
            return (0, "2\n")
        if args[:1] == ("pull",):
            return (0, "Fast-forward\n")
        return (0, "")

    sent.clear(); _upd_calls[:] = []; _reloaded[:] = []
    wd._git = _upd_happy
    wd._update_suite = lambda: (True, "==== 1315 passed, 0 failed ====", frozenset())
    wd.handle_update("!update go", "CID_OM")
    check("update go: a clean tree pulls ff-only, reports old -> new, reloads",
          any("updated" in s[1] and "eee5555" in s[1] for s in sent)
          and _reloaded == ["CID_OM"]
          and any(c[:2] == ("pull", "--ff-only") for c in _upd_calls))

    def _suite_seq(*results):
        # baseline call first, post-pull call second; extras repeat the last
        it = {"n": 0}
        def f():
            r = results[min(it["n"], len(results) - 1)]
            it["n"] += 1
            return r
        return f

    # a failure the UPDATE introduces rolls back - and is named
    sent.clear(); _upd_calls[:] = []; _reloaded[:] = []; _seq["n"] = 0
    wd._update_suite = _suite_seq(
        (True, "==== all green ====", frozenset()),
        (False, "==== 1 failed ====", frozenset({"the new thing broke"})))
    wd.handle_update("!update go", "CID_OM")
    check("update go: a failure the update INTRODUCES rolls back and does not reload",
          any("rolled back" in s[1].lower() for s in sent)
          and any(c[:2] == ("reset", "--hard") for c in _upd_calls)
          and _reloaded == [])
    check("update go: ...and the broken check is NAMED in the rollback",
          any("the new thing broke" in s[1] for s in sent))
    # pre-existing LOCAL failures are the machine's housekeeping, not the
    # update's fault - the gate judges the delta (proven live 2026-08-15:
    # a second instance's untidy memory blocked its first !update go)
    sent.clear(); _upd_calls[:] = []; _reloaded[:] = []; _seq["n"] = 0
    wd._update_suite = _suite_seq(
        (False, "==== 2 failed ====", frozenset({"memory budget", "notes stale"})),
        (False, "==== 2 failed ====", frozenset({"memory budget", "notes stale"})))
    wd.handle_update("!update go", "CID_OM")
    check("update go: pre-existing local failures do not block - the gate judges the delta",
          _reloaded == ["CID_OM"]
          and any("baseline" in s[1] for s in sent)
          and any("pre-existing" in s[1] for s in sent)
          and not any(c[:2] == ("reset", "--hard") for c in _upd_calls))
    # == boot release notice ==================================================
    # Owner ask 2026-08-16: when the watchdog STARTS, say once what origin/main
    # has and how to apply it - never apply on its own. "Once" is per origin
    # tip, stamped in state, so a crash-looping service (a boot per self-heal)
    # cannot break the same news every minute.
    print("== boot release notice ==")
    _bn_map = {"c_orch": T("omnius", "orchestrator")}
    _bn_behind = [
        (("rev-parse", "--is-inside-work-tree"), (0, "true")),
        (("fetch",), (0, "")),
        (("rev-list",), (0, "2\n")),
        (("rev-parse", "origin/main"), (0, "feedbeef" * 5 + "\n")),
        (("log",), (0, "bbb2222 fix a\nccc3333 fix b\n")),
    ]
    sent.clear(); _upd_calls[:] = []; _reloaded[:] = []
    wd._git = _script_git(_bn_behind)
    (wd.WD_STATE / "update-announced.json").unlink(missing_ok=True)
    wd.update_boot_notice(_bn_map)
    check("boot notice: behind -> posts the commits and the go verb",
          len(sent) == 1 and "2 new commit(s)" in sent[0][1]
          and "!update go" in sent[0][1] and "bbb2222" in sent[0][1])
    check("boot notice: it never pulls or reloads on its own",
          not any(c[:1] == ("pull",) for c in _upd_calls) and not _reloaded)
    wd.update_boot_notice(_bn_map)
    check("boot notice: the same origin tip is announced only once",
          len(sent) == 1)
    wd._git = _script_git([
        (("rev-parse", "--is-inside-work-tree"), (0, "true")),
        (("fetch",), (0, "")),
        (("rev-list",), (0, "3\n")),
        (("rev-parse", "origin/main"), (0, "cafef00d" * 5 + "\n")),
        (("log",), (0, "ddd4444 x\neee5555 y\nfff6666 z\n")),
    ])
    wd.update_boot_notice(_bn_map)
    check("boot notice: a NEW tip is fresh news and posts again",
          len(sent) == 2 and "3 new commit(s)" in sent[-1][1])
    sent.clear()
    wd._git = _script_git([
        (("rev-parse", "--is-inside-work-tree"), (0, "true")),
        (("fetch",), (1, "fatal: unable to access github")),
    ])
    wd.update_boot_notice(_bn_map)
    check("boot notice: offline is a log line, not a page", not sent)
    wd._git = _script_git([
        (("rev-parse", "--is-inside-work-tree"), (0, "true")),
        (("fetch",), (0, "")),
        (("rev-list",), (0, "0\n")),
    ])
    wd.update_boot_notice(_bn_map)
    check("boot notice: current -> silent", not sent)
    wd._git = _script_git([(("rev-parse", "--is-inside-work-tree"), (1, "fatal"))])
    wd.update_boot_notice(_bn_map)
    check("boot notice: an unattached install stays silent at boot", not sent)
    _src_bn = (real_root / "tools" / "discord" / "watchdog.py").read_text(encoding="utf-8")
    check("boot notice: wired into main() right after hello_post",
          "update_boot_notice(mapping)" in _src_bn.split("hello_post(mapping)")[-1][:120])
    # A watchdog can run for weeks with no boot, so boot-only would never fire
    # on exactly the machines that drift furthest - the poll loop re-checks
    # daily, same function, same per-tip stamp.
    check("boot notice: the poll loop re-checks daily (same function, both sites)",
          _src_bn.count("update_boot_notice(mapping)") == 3   # def + boot + poll
          and "RELEASE_CHECK_SECONDS" in _src_bn.split("rotate_log()")[-1][:500])
    check("boot notice: every run stamps the process clock for that cadence",
          wd._release_last[0] > 0)
    (wd.WD_STATE / "update-announced.json").unlink(missing_ok=True)

    wd._git, wd._update_suite, wd.do_reload = _real_git, _real_us, _real_dr
    wd._update_restamp = _real_restamp
    sent.clear()
    # the build side of the same story, pinned in source
    _packsrc2 = (real_root / "pack.ps1").read_text(encoding="utf-8")
    _instsrc2 = (real_root / "install.ps1").read_text(encoding="utf-8")
    check("pack stamps the zip with its birth commit", "RELEASE-COMMIT" in _packsrc2)
    check("install attaches a zip install to the repo for updates",
          "github (updates)" in _instsrc2 and "reset --mixed" in _instsrc2)
    check("...and the stamp itself can never be committed",
          "RELEASE-COMMIT" in (real_root / ".gitignore").read_text(encoding="utf-8"))

    # == update handshake (docs\OBSERVABILITY.md O2) ===========================
    # !update validates before the reload; this half validates AFTER: the new
    # watchdog must prove it took over, or the old commit takes back over on
    # its own. Everything through stubs - _reexec_self would replace THIS
    # process's image, and the suite must never re-exec itself.
    print("== update handshake ==")
    _real_reexec = wd._reexec_self
    _real_restamp2 = wd._update_restamp
    _real_git3, _real_us3, _real_dr3 = wd._git, wd._update_suite, wd.do_reload
    _hs_seq, _hs_git = [], []
    wd._reexec_self = lambda: _hs_seq.append("reexec")
    wd._update_restamp = lambda: _hs_seq.append("restamp")
    P = wd._pending_path()
    P.unlink(missing_ok=True)
    wd._git = lambda *a, timeout=60: (_hs_git.append(a) or (0, ""))
    sent.clear()
    wd.update_pending_boot()
    wd.update_pending_confirm()
    check("handshake: a normal boot with no pending file does none of this",
          not P.exists() and sent == [] and _hs_seq == [])
    # !update go writes the handoff BEFORE the re-exec, with both full commits
    _hs2 = {"n": 0}

    def _hs_happy(*args, timeout=60):
        _hs_git.append(args)
        if args[:2] == ("rev-parse", "--is-inside-work-tree"):
            return (0, "true")
        if args[:3] == ("rev-parse", "--short", "HEAD"):
            _hs2["n"] += 1
            return (0, "aaa1111\n" if _hs2["n"] == 1 else "eee5555\n")
        if args[:2] == ("rev-parse", "HEAD"):
            return (0, ("aaa1111" if _hs2["n"] <= 1 else "eee5555") + "0" * 33 + "\n")
        if args[:1] == ("rev-list",):
            return (0, "2\n")
        if args[:1] == ("pull",):
            return (0, "Fast-forward\n")
        return (0, "")

    _pend_at_reload = []
    wd.do_reload = lambda cid, announce=True: _pend_at_reload.append(P.exists())
    wd._update_suite = lambda: (True, "==== ok ====", frozenset())
    wd._git = _hs_happy
    wd.handle_update("!update go", "CID_OM")
    _hrec = json.loads(P.read_text(encoding="utf-8"))
    check("handshake: go writes the pending handoff before the re-exec",
          _pend_at_reload == [True] and _hrec.get("bootAttempts") == 0
          and _hrec.get("fromCommit", "").startswith("aaa1111")
          and _hrec.get("toCommit", "").startswith("eee5555")
          and _hrec.get("channelId") == "CID_OM")
    wd.update_pending_boot()
    check("handshake: bootAttempts counts every boot while pending",
          json.loads(P.read_text(encoding="utf-8")).get("bootAttempts") == 1)
    sent.clear()
    wd.update_pending_confirm()
    check("handshake: a healthy tick posts the checkmark once, naming both commits",
          bool(sent) and "✅" in sent[-1][1] and "aaa1111" in sent[-1][1]
          and "eee5555" in sent[-1][1] and not P.exists())
    sent.clear()
    wd.update_pending_confirm()
    check("handshake: ...and only once", sent == [])
    # the third still-unhealthy boot reverts to fromCommit
    wd._git = lambda *a, timeout=60: (_hs_git.append(a) or (0, ""))
    wd.write_json_atomic(P, {"fromCommit": "f" * 40, "toCommit": "b" * 40,
                             "channelId": "CID_OM", "startedAt": now(),
                             "startedTs": _time.time(), "bootAttempts": 2})
    _hs_git[:] = []; _hs_seq[:] = []
    wd.update_pending_boot()
    _hrec = json.loads(P.read_text(encoding="utf-8"))
    check("handshake: the third failed boot reverts to fromCommit",
          any(a[:2] == ("reset", "--hard") and a[2:3] == ("f" * 40,) for a in _hs_git)
          and _hrec.get("reverted") is True)
    check("handshake: revert restamps, then re-execs - in that order",
          _hs_seq == ["restamp", "reexec"])
    sent.clear()
    wd.update_pending_boot()
    check("handshake: a reverted record stops the boot counting",
          "bootAttempts" not in json.loads(P.read_text(encoding="utf-8")))
    wd.update_pending_confirm()
    check("handshake: the old code breaks the bad news, naming both commits",
          bool(sent) and "⛔" in sent[-1][1] and "reverted" in sent[-1][1]
          and "fffffff" in sent[-1][1] and "bbbbbbb" in sent[-1][1]
          and not P.exists())
    # aged pending (booted, then sat deaf until the DEAF exit) reverts too
    wd.write_json_atomic(P, {"fromCommit": "f" * 40, "toCommit": "b" * 40,
                             "channelId": "CID_OM", "startedAt": now(),
                             "startedTs": _time.time() - 700, "bootAttempts": 0})
    _hs_git[:] = []; _hs_seq[:] = []
    wd.update_pending_boot()
    check("handshake: a deaf-aged pending file reverts even on its first recount",
          any(a[:2] == ("reset", "--hard") for a in _hs_git))
    P.unlink(missing_ok=True)
    # the main loop is actually wired to both halves (call sites, not defs:
    # the 4-space call in main's startup, the 16-space call on the healthy tick)
    _wd_now = (real_root / "tools" / "discord" / "watchdog.py").read_text(encoding="utf-8")
    check("handshake: boot and confirm are wired into main()",
          "\n    update_pending_boot()" in _wd_now
          and "\n                update_pending_confirm()" in _wd_now)
    wd._git, wd._update_suite, wd.do_reload = _real_git3, _real_us3, _real_dr3
    wd._update_restamp, wd._reexec_self = _real_restamp2, _real_reexec
    sent.clear()

    # == !trace (docs\OBSERVABILITY.md O1) =====================================
    # One chain's whole story from state alone - ledgers, gate records, loop
    # files. Never the logs.
    print("== !trace ==")
    _real_ttl2, _real_gatereq2 = wd._hop_ttl, wd._gate_required
    wd._hop_ttl = lambda: 2
    wd._gate_required = lambda: True
    wd._desk_id_cache.clear()
    sent.clear()
    ptr1 = dmail("alpha.app", {"to": "alpha.web", "text": "trace me",
                               "origin": {"channelId": "id-alpha-app", "from": "owner"}},
                 "1700000000060")
    wd.deliver_desk_mail(dm, "alpha.app", ptr1, json.loads(ptr1.read_text(encoding="utf-8")))
    t_tr = json.loads(sorted((wd.INBOX / "alpha.web").glob("*.json"))[-1]
                      .read_text(encoding="utf-8")).get("thread")
    ptr2 = dmail("alpha.web", {"to": "alpha.app", "text": "traced back", "thread": t_tr},
                 "1700000000061")
    wd.deliver_desk_mail(dm, "alpha.web", ptr2, json.loads(ptr2.read_text(encoding="utf-8")))
    _led_tr = wd._load_thread(t_tr)
    _d0 = (_led_tr.get("deliveries") or [{}])[0]
    check("trace: a chain's deliveries carry from/to/ts since O1",
          isinstance(_d0, dict) and _d0.get("from") == "alpha.app"
          and _d0.get("to") == "alpha.web" and str(_d0.get("ts", "")).endswith("Z")
          and _d0.get("reply") is False)
    sent.clear()
    wd.handle_trace(f"!trace {t_tr}", "CID_OM")
    _tr_out = sent[-1][1]
    check("trace: a spent hop and a free reply are labelled differently",
          "[hop]" in _tr_out and "[reply]" in _tr_out
          and "alpha.app → alpha.web" in _tr_out)
    check("trace: the chain's origin and budget are on the screen",
          "id-alpha-app" in _tr_out and "1 spent · 1 free" in _tr_out)
    # old bare-string deliveries (a mid-flight ledger) still render
    _legacy = {"id": "t-legacy-alpha.app", "origin": None, "hopsLeft": 1,
               "deliveries": ["dm-alpha.app-1699999999999"], "edges": [],
               "lastDeliveredTo": "alpha.web", "startedAt": now(), "lastAt": now(),
               "closed": None}
    wd._save_thread(_legacy)
    sent.clear()
    wd.handle_trace("!trace t-legacy-alpha.app", "CID_OM")
    check("trace: old bare-string deliveries still render (mid-flight upgrade)",
          "dm-alpha.app-1699999999999" in sent[-1][1]
          and "pre-trace" in sent[-1][1])
    # a held gate shows WAITING with its deadline; a dropped one shows DROPPED
    pg_tr = dmail("alpha.app", {"to": "beta.app", "text": "cross for trace"},
                  "1700000000062")
    wd.deliver_desk_mail(dm, "alpha.app", pg_tr, json.loads(pg_tr.read_text(encoding="utf-8")))
    _g_tr = wd.pending_gates()[-1]
    sent.clear()
    wd.handle_trace(f"!trace {_g_tr['thread']}", "CID_OM")
    check("trace: a waiting gate hold appears with its deadline",
          "WAITING" in sent[-1][1] and "drops at" in sent[-1][1])
    wd._resolve_gate(dm, _g_tr, "deny", "timeout")
    sent.clear()
    wd.handle_trace(f"!trace {_g_tr['thread']}", "CID_OM")
    check("trace: a dropped gate hold shows its outcome and old deadline",
          "DROPPED" in sent[-1][1] and "deadline was" in sent[-1][1])
    # loops: fires against budget
    _lp_tr = sch.open_loop("orchestrator", 3)
    _lp_tr["fired"] = 2
    sch.save_loop(_lp_tr)
    sent.clear()
    wd.handle_trace(f"!trace {_lp_tr['id']}", "CID_OM")
    check("trace: a loop trace shows fires against budget",
          "2/3" in sent[-1][1] and "orchestrator" in sent[-1][1])
    # bare listing, newest first (two hand-stamped ledgers with known order)
    for _tid, _at in (("t-newest-alpha.app", "2099-01-02T00:00:00Z"),
                      ("t-older-alpha.app", "2099-01-01T00:00:00Z")):
        (wd.THREADS / f"{_tid}.json").write_text(json.dumps(
            {"id": _tid, "origin": {"session": "alpha.app"}, "hopsLeft": 1,
             "deliveries": [], "edges": [], "lastDeliveredTo": None,
             "startedAt": _at, "lastAt": _at, "closed": None}), encoding="utf-8")
    sent.clear()
    wd.handle_trace("!trace", "CID_OM")
    _ls = sent[-1][1]
    check("trace: bare !trace lists recent chains newest-first",
          "CHAINS" in _ls and 0 < _ls.index("t-newest-alpha.app") < _ls.index("t-older-alpha.app"))
    check("trace: loops ride along in the bare listing", "run 2/3" in _ls)
    sent.clear()
    wd.handle_trace("!trace nope-999", "CID_OM")
    check("trace: an unknown id says so and lists what exists",
          "nothing called" in sent[-1][1] and "CHAINS" in sent[-1][1])
    sent.clear(); spawned.clear()
    _r_tr = wd.handle_message(msg("999999999999999999", "!trace"), "CID_OM",
                              T("omnius", "orchestrator"), me, dm)
    check("trace: !trace is a control verb and spawns nothing",
          _r_tr == "control" and spawned == [] and bool(sent))
    (wd.THREADS / "t-newest-alpha.app.json").unlink(missing_ok=True)
    (wd.THREADS / "t-older-alpha.app.json").unlink(missing_ok=True)
    wd._hop_ttl, wd._gate_required = _real_ttl2, _real_gatereq2
    sent.clear()

    # == permission escalation =================================================
    # Relaying a prompt to Discord is what lets the profile be TIGHTENED instead
    # of widened until prompts stop. Silence must never mean "allow".
    print("== permission escalation ==")
    wd.PERMS = SAND / "permissions"
    wd.PERMS.mkdir(parents=True, exist_ok=True)

    def ask(rid, code, tool="Bash", session="alpha.app"):
        (wd.PERMS / f"{rid}.json").write_text(json.dumps(
            {"id": rid, "code": code, "session": session, "tool": tool,
             "detail": "npm install"}), encoding="utf-8")

    def verdict_of(rid):
        p = wd.PERMS / f"{rid}.answer"
        return json.loads(p.read_text(encoding="utf-8"))["behavior"] if p.exists() else None

    check("permissions: nothing pending -> ordinary message untouched",
          wd.answer_permission("ok") is None)
    ask("r1", "aaa111")
    check("permissions: unrelated chat is not swallowed as a verdict",
          wd.answer_permission("what is the status of the build?") is None)
    check("permissions: 'ok' allows", bool(wd.answer_permission("ok")))
    check("permissions: verdict written as allow", verdict_of("r1") == "allow")
    check("permissions: request cleared once answered", not (wd.PERMS / "r1.json").exists())
    ask("r2", "bbb222")
    wd.answer_permission("no")
    check("permissions: 'no' denies", verdict_of("r2") == "deny")
    # two pending at once must not be answered by a bare yes
    # 2026-08-02, verbatim: "I could not write 20 times ok in discord". One
    # request ("read this website") became SIX WebFetch asks in six seconds,
    # and a bare ok was refused as ambiguous every time. Six asks from one
    # intention deserve one answer.
    ask("r3", "ccc333", session="alpha.app")
    ask("r4", "ddd444", session="alpha.app")
    msg = wd.answer_permission("ok")
    check("permissions: one 'ok' answers ALL of a single desk's pending asks",
          verdict_of("r3") == "allow" and verdict_of("r4") == "allow")
    check("...and says so, naming how many", bool(msg) and "all 2" in msg)
    check("...clearing every request file", not (wd.PERMS / "r3.json").exists()
          and not (wd.PERMS / "r4.json").exists())

    # Different DESKS are different decisions - those still need a code.
    ask("r5", "eee555", session="alpha.app")
    ask("r6", "fff666", session="beta.web")
    msg = wd.answer_permission("ok")
    check("permissions: asks on DIFFERENT desks still require a code",
          bool(msg) and "different desks" in msg
          and verdict_of("r5") is None and verdict_of("r6") is None)
    wd.answer_permission("ok fff666")
    check("permissions: a code still picks exactly one request",
          verdict_of("r6") == "allow" and verdict_of("r5") is None)
    wd.answer_permission("ok")          # r5 alone now
    check("permissions: the last one falls back to the single-pending path",
          verdict_of("r5") == "allow")
    _relay2 = (_rr / "tools" / "discord" / "permission_relay.py").read_text(encoding="utf-8")
    check("the ask itself tells him one answer covers the whole batch",
          "covers all" in _relay2)

    import permission_relay as pr
    check("relay: cwd -> orchestrator", pr.session_id_for(pr.ROOT) == "orchestrator")
    check("relay: cwd -> project session",
          pr.session_id_for(pr.ROOT / "projects" / "alpha" / "app") == "alpha.app")
    check("relay: cwd -> tool session", pr.session_id_for(pr.ROOT / "tools" / "discord") == "tool.discord")
    check("relay: cwd -> daybook", pr.session_id_for(pr.ROOT / "daybook") == "daybook")
    check("relay: unrelated cwd -> no desk", pr.session_id_for("C:\\Windows") is None)

    # -- escalation, end to end -----------------------------------------------
    # Until now only the two HALVES were tested: answer_permission() above, and
    # the relay's cwd mapping. main() - the gate, the wait loop, the decision it
    # actually prints - was never exercised, which is precisely the half that
    # has never been seen to work ("nobody has ever successfully answered one").
    # These drive BOTH halves against one sandbox, so the code the relay
    # publishes is the code answer_permission has to match.
    print("== escalation, end to end ==")
    import threading as _th, io as _io2, contextlib as _ctx2
    pr.ROOT, pr.STATE = SAND, SAND
    pr.SESSIONS, pr.OUTBOX, pr.PERMS = wd.SESSIONS, wd.OUTBOX, wd.PERMS
    _os.environ["PERMISSION_ESCALATION_SECONDS"] = "6"

    def _bus_claim(sid, channel="app", secs_ago=2):
        # A REAL live pid and THIS machine: since the run model the relay's gate
        # is pid liveness, not heartbeat freshness (there is no heartbeat).
        seen = (_dt2.now(_tz2.utc) - _td2(seconds=secs_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
        (wd.SESSIONS / f"{sid}.json").write_text(json.dumps(
            {"role": "project", "pid": _os.getpid(), "startedAt": seen,
             "lastSeenAt": seen, "machine": api.MACHINE, "discordChannel": channel}),
            encoding="utf-8")

    def _escalate(tool_use_id, answer_with=None, delay=0.6, tool="Bash",
                  tool_input=None, cwd=None):
        """Run the hook exactly as Claude Code does; optionally answer it midway."""
        out = _io2.StringIO()
        t = None
        if answer_with is not None:
            def _reply():
                _t2.sleep(delay)
                wd.answer_permission(answer_with)     # the WATCHDOG half
            t = _th.Thread(target=_reply, daemon=True); t.start()
        old = sys.stdin
        sys.stdin = _io2.StringIO(json.dumps({
            "cwd": cwd or str(SAND / "projects" / "demo-app" / "app"),
            "tool_name": tool, "tool_use_id": tool_use_id,
            "tool_input": tool_input if tool_input is not None else {"command": "rm -rf build"},
        }))
        try:
            with _ctx2.redirect_stdout(out):
                rc = pr.main()
        finally:
            sys.stdin = old
            if t:
                t.join(timeout=5)
        return rc, out.getvalue()

    def _decision(out):
        """The hook's verdict, ignoring anything else on the shared stdout.

        In production the relay is its own process and prints only this. Here the
        answering thread runs wd.answer_permission() inside the same redirect, so
        the watchdog's log line lands in the same buffer - a test artifact, not a
        product bug. Take the last JSON object on its own line.
        """
        for line in reversed(out.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except ValueError:
                    pass
        return {}

    _obox = wd.OUTBOX / "demo-app.app"
    for _d in (wd.PERMS, _obox):
        _d.mkdir(parents=True, exist_ok=True)

    def _clear():
        for f in list(wd.PERMS.glob("*")) + list(_obox.glob("*")):
            f.unlink(missing_ok=True)

    # A desk nobody drives remotely must pay NO delay and produce no traffic.
    _clear()
    (wd.SESSIONS / "demo-app.app.json").unlink(missing_ok=True)
    _t0 = _t2.time()
    rc, out = _escalate("tid-nobus")
    check("not bus-connected: returns instantly, prints nothing, sends nothing",
          rc == 0 and out == "" and not list(_obox.glob("*")) and (_t2.time() - _t0) < 2)

    # A claim with no Discord channel is a local desk - same rule.
    _bus_claim("demo-app.app", channel=None)
    rc, out = _escalate("tid-nochan")
    check("claimed but not bound to a channel: still no escalation", out == "")

    # The real path: the owner says ok, and the session is ALLOWED to proceed.
    _clear(); _bus_claim("demo-app.app")
    rc, out = _escalate("tid-aaa111", answer_with="ok")
    _dec = _decision(out)
    check("owner replies 'ok' -> the hook prints an ALLOW decision",
          _dec.get("hookSpecificOutput", {}).get("decision", {}).get("behavior") == "allow")
    check("...naming the right hook event",
          _dec.get("hookSpecificOutput", {}).get("hookEventName") == "PermissionRequest")

    _clear(); _bus_claim("demo-app.app")
    rc, out = _escalate("tid-bbb222", answer_with="no")
    _dec = _decision(out)
    check("owner replies 'no' -> the hook prints a DENY decision",
          _dec.get("hookSpecificOutput", {}).get("decision", {}).get("behavior") == "deny")

    # The question that reaches the phone has to be answerable FROM the phone.
    _clear(); _bus_claim("demo-app.app")
    rc, out = _escalate("tid-ccc333", answer_with="ok")
    # (envelopes are consumed by answer flow; re-run without an answer to read one)
    _clear(); _bus_claim("demo-app.app")
    _os.environ["PERMISSION_ESCALATION_SECONDS"] = "3"
    rc, out = _escalate("tid-ddd444")
    envs = [json.loads(f.read_text(encoding="utf-8")) for f in sorted(_obox.glob("*.json"))]
    asked = next((e for e in envs if "permission needed" in e.get("text", "")), None)
    check("the ask reaches the owner as a message", asked is not None)
    check("the ask carries the code the owner must reply with",
          asked is not None and "ddd444" in asked["text"])
    check("the ask says what will run", asked is not None and "rm -rf build" in asked["text"])
    check("the ask spells out how to answer",
          asked is not None and "ok" in asked["text"] and "no" in asked["text"])

    # Silence must never mean yes.
    check("no answer -> prints NOTHING, so the local dialog still guards it",
          _decision(out) == {})
    check("a timed-out request leaves a durable stall marker",
          (wd.PERMS / "demo-app.app.stalled").is_file())
    check("...and says in the channel that it fell back",
          any("no answer" in e.get("text", "") for e in envs))

    # The next request proves the desk is alive again - the marker must not
    # outlive the condition it describes.
    _bus_claim("demo-app.app")
    _os.environ["PERMISSION_ESCALATION_SECONDS"] = "6"
    _escalate("tid-eee555", answer_with="ok")
    check("a new request clears the stale stall marker",
          not (wd.PERMS / "demo-app.app.stalled").is_file())

    # A secret in a command line must not be broadcast to a chat channel.
    _clear(); _bus_claim("demo-app.app")
    _os.environ["PERMISSION_ESCALATION_SECONDS"] = "3"
    _escalate("tid-fff666", tool_input={"command": "curl -H 'Authorization: Bot MTA5.abcdefghijklmnopqrstuvwxyz123456'"})
    leaked = [e for e in (json.loads(f.read_text(encoding="utf-8")) for f in _obox.glob("*.json"))
              if "abcdefghijklmnopqrstuvwxyz123456" in e.get("text", "")]
    check("a token in the command is redacted before it reaches Discord", leaked == [])

    _clear()
    (wd.SESSIONS / "demo-app.app.json").unlink(missing_ok=True)
    _os.environ.pop("PERMISSION_ESCALATION_SECONDS", None)

    # ---------------------------------------------------------------- gateway
    # The websocket client is hand-rolled (stdlib only, see gateway.py), so the
    # framing and the protocol state machine are OURS to get right. Everything
    # here runs offline against a scripted fake socket.
    print("== gateway: RFC 6455 framing ==")
    import gateway as gwm
    # The example key/accept pair straight out of RFC 6455 sec 1.3. If this
    # breaks, every handshake breaks.
    check("accept_key matches the RFC example",
          gwm.accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")
    for size, label in ((5, "tiny"), (200, "16-bit length"), (70000, "64-bit length")):
        payload = bytes(range(256)) * (size // 256) + bytes(size % 256)
        frame = gwm.encode_frame(gwm.WS_TEXT, payload)
        got = gwm.parse_frame(frame)
        check(f"frame round-trip, {label}",
              got is not None and got[1] == gwm.WS_TEXT and got[2] == payload
              and got[3] == len(frame))
    # A client frame that is not masked is a protocol error; Discord hangs up.
    check("client frames are masked", bool(gwm.encode_frame(gwm.WS_TEXT, b"hi")[1] & 0x80))
    _f = gwm.encode_frame(gwm.WS_TEXT, b"abcdefghij")
    check("partial buffer parses to None (nothing consumed)",
          all(gwm.parse_frame(_f[:n]) is None for n in range(1, len(_f))))
    # A frame arriving in two recv() chunks must survive the split - this is the
    # case that makes a read timeout mid-frame harmless.
    check("frame completes once the rest of the bytes arrive",
          gwm.parse_frame(_f[:4] + _f[4:])[2] == b"abcdefghij")
    _server = bytes([0x81, 3]) + b"abc"      # unmasked, as servers send
    check("unmasked server frame parses", gwm.parse_frame(_server)[2] == b"abc")

    print("== gateway: protocol ==")
    check("intents ask for MESSAGE_CONTENT (or every envelope is blank)",
          gwm.INTENTS & (1 << 15) and gwm.INTENTS & (1 << 9) and gwm.INTENTS & 1)
    check("4014 is known-fatal with the portal fix in the message",
          4014 in gwm.FATAL_CLOSE and "MESSAGE CONTENT" in gwm.FATAL_CLOSE[4014])
    check("4004 (bad token) is fatal too, not retried forever", 4004 in gwm.FATAL_CLOSE)

    class FakeWS:
        """Scripted socket: each recv_json pops the next item; an Exception is raised."""
        last = None
        def __init__(self, url, timeout=30):
            self.url, self.sent, self.closed = url, [], False
            self.script = list(FakeWS.script)
            FakeWS.last = self
        def connect(self): pass
        def send_text(self, t): self.sent.append(json.loads(t))
        def recv_json(self, timeout):
            if not self.script:
                raise gwm.GatewayClosed("script exhausted", 1000)
            item = self.script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        def close(self): self.closed = True

    HELLO = {"op": 10, "d": {"heartbeat_interval": 45000}}
    READY = {"op": 0, "s": 1, "t": "READY",
             "d": {"session_id": "sess123", "resume_gateway_url": "wss://resume.example",
                   "user": {"username": "Omnius"}}}
    def msg(mid, cid, content="hi"):
        return {"op": 0, "s": 2, "t": "MESSAGE_CREATE",
                "d": {"id": mid, "channel_id": cid, "content": content,
                      "author": {"id": "999999999999999999"}}}

    FakeWS.script = [HELLO, READY, msg("100", "CID_A"),
                     gwm.GatewayClosed("done", 1000)]
    g = gwm.Gateway("tok", log=lambda *a: None, ws_factory=FakeWS)
    try:
        g._session()
    except gwm.GatewayClosed:
        pass
    _ident = [s for s in FakeWS.last.sent if s.get("op") == 2]
    check("IDENTIFY is sent with our intents", bool(_ident) and _ident[0]["d"]["intents"] == gwm.INTENTS)
    check("IDENTIFY announces presence (free health indicator on the phone)",
          bool(_ident) and _ident[0]["d"]["presence"]["status"] == "online")
    check("READY records the session for resuming", g._session_id == "sess123")
    check("READY records the resume url", g._resume_url == "wss://resume.example")
    check("MESSAGE_CREATE lands on the queue", g.events.qsize() == 1)
    check("queued payload is REST-shaped (handle_message takes it as-is)",
          g.events.get()["id"] == "100")

    # A resume must reuse the session, not start a new one - that is what makes
    # a dropped socket lossless rather than a gap.
    FakeWS.script = [HELLO, {"op": 0, "s": 3, "t": "RESUMED", "d": {}},
                     gwm.GatewayClosed("done", 1000)]
    try:
        g._session()
    except gwm.GatewayClosed:
        pass
    _res = [s for s in FakeWS.last.sent if s.get("op") == 6]
    check("second connect RESUMEs with the stored session + seq",
          bool(_res) and _res[0]["d"]["session_id"] == "sess123" and _res[0]["d"]["seq"] == 2)
    check("resume uses the resume_gateway_url Discord gave us",
          FakeWS.last.url.startswith("wss://resume.example"))

    # op9 with d=false means the session is gone; keeping it would loop forever
    # RESUMEing into another invalidation.
    g2 = gwm.Gateway("tok", log=lambda *a: None, ws_factory=FakeWS)
    FakeWS.script = [HELLO, READY, {"op": 9, "d": False}]
    try:
        g2._session()
    except gwm.GatewayClosed:
        pass
    check("INVALID_SESSION(false) forgets the session so the retry IDENTIFYs fresh",
          g2._session_id is None and g2._seq is None)

    g3 = gwm.Gateway("tok", log=lambda *a: None, ws_factory=FakeWS)
    FakeWS.script = [HELLO, READY, {"op": 7}]
    _raised = ""
    try:
        g3._session()
    except gwm.GatewayClosed as e:
        _raised = str(e)
    check("op7 RECONNECT ends the session so the loop reconnects", "reconnect" in _raised)

    # Zombie connection: socket open, heartbeats unanswered. Interval 0 makes the
    # deadline immediate so this needs no sleeping.
    g4 = gwm.Gateway("tok", log=lambda *a: None, ws_factory=FakeWS)
    FakeWS.script = [{"op": 10, "d": {"heartbeat_interval": 0}}, READY, None, None, None]
    _zombie = ""
    try:
        g4._session()
    except gwm.GatewayClosed as e:
        _zombie = str(e)
    check("un-acked heartbeat drops the connection instead of going quietly deaf",
          "zombie" in _zombie)

    # A close nobody can fix must stop the thread: the watchdog stays on REST
    # and the log says why. A bad token is that case - it stays wrong.
    g5 = gwm.Gateway("tok", log=lambda *a: None, ws_factory=FakeWS)
    FakeWS.script = [gwm.GatewayClosed("nope", 4004)]
    g5._run_forever()
    check("a bad token stops retrying and records why",
          g5.fatal and "DISCORD_BOT_TOKEN is wrong" in g5.fatal and not g5.connected)
    # An unticked intent is NOT that case, and treating it as one cost a real
    # evening: a fresh instance sat on 60s polling with its desk explaining
    # that a watchdog restart was needed, when the fix was a checkbox in a
    # browser (2026-08-15). Once ticked, push must come back on its own.
    check("...but an unticked intent is retried, because a checkbox fixes it",
          g5.retry_delay_for(4014) == gwm.FIXABLE_RETRY_SECONDS)
    check("...and a wrong token is not retried, because nothing external fixes it",
          g5.retry_delay_for(4004) is None)
    check("the retry interval is minutes, not a hot loop",
          gwm.FIXABLE_RETRY_SECONDS >= 300)

    print("== watchdog: two transports, one handler ==")
    check("SeenIds suppresses a repeat", wd.SeenIds().add("1") and not (
        (lambda s: (s.add("1"), s.add("1"))[1])(wd.SeenIds())))
    _s = wd.SeenIds(size=3)
    for i in "abcd":
        _s.add(i)
    check("SeenIds forgets the oldest once full", "a" not in _s and "d" in _s and len(_s) == 3)

    _cursor_at_handle = {}
    _handled = []
    _real_handle = wd.handle_message
    _lastids_file = SAND / "watchdog" / "last_ids.json"
    _lastids_file.parent.mkdir(parents=True, exist_ok=True)
    _ids = {}
    def _persist():
        wd.write_json_atomic(_lastids_file, _ids)
    def _fake_handle(m, cid, target, me, mapping):
        # Read the cursor from DISK: the durability rule is that it is written
        # before the side effect, because !reload re-execs mid-handling.
        _cursor_at_handle[m["id"]] = json.loads(_lastids_file.read_text(encoding="utf-8")).get(cid)
        _handled.append(m["id"])
        return "delivered"
    wd.handle_message = _fake_handle
    _seen = wd.SeenIds()
    _map = {"CID_A": T("app", "demo-app.app")}
    wd.deliver({"id": "500"}, "CID_A", _map["CID_A"], {"id": "bot"}, _map, _ids, _persist, _seen)
    check("deliver persists the cursor BEFORE handling (survives a !reload re-exec)",
          _cursor_at_handle.get("500") == "500")
    _r = wd.deliver({"id": "500"}, "CID_A", _map["CID_A"], {"id": "bot"}, _map, _ids, _persist, _seen)
    check("the same message is never handled twice across transports",
          _r == "duplicate" and _handled == ["500"])

    # The overlap this guards: gateway pushes while a REST sweep is in flight.
    _handled.clear()
    _seen2 = wd.SeenIds()
    _ids2 = {"CID_A": "0"}
    api.messages_after = lambda cid, after, limit=50: [{"id": "600"}]
    api.latest_message_id = lambda cid: "0"
    wd.api.messages_after, wd.api.latest_message_id = api.messages_after, api.latest_message_id
    _deaf, _delivered = wd.rest_sweep(_map, {"id": "bot"}, _ids2, _persist, _seen2)
    check("rest_sweep delivers and reports what it delivered",
          _delivered == 1 and _deaf == 0 and _handled == ["600"])
    class _Q:
        def __init__(self, items): self.items = list(items)
        def get(self, timeout=None):
            if not self.items:
                import queue as _q
                raise _q.Empty
            return self.items.pop(0)
    class _GW:
        def __init__(self, items): self.events = _Q(items)
    _handled.clear()
    wd.drain_gateway(_GW([{"id": "600", "channel_id": "CID_A"}]),
                     _time.time() + 1, _map, {"id": "bot"}, _ids2, _persist, _seen2)
    check("a message already rescued by the sweep is not re-delivered from the queue",
          _handled == [])
    _handled.clear()
    wd.drain_gateway(_GW([{"id": "700", "channel_id": "NOT_OURS"},
                          {"id": "701", "channel_id": "CID_A"}]),
                     _time.time() + 1, _map, {"id": "bot"}, _ids2, _persist, _seen2)
    check("gateway messages from channels we do not own are ignored", _handled == ["701"])
    wd.handle_message = _real_handle

    _wsrc = (HERE / "watchdog.py").read_text(encoding="utf-8")
    # The property that makes a hand-rolled websocket an acceptable risk: REST
    # never stops running, so a dropped frame costs latency, not a message.
    check("the REST sweep still runs while the gateway is live (backstop, not replaced)",
          "RECONCILE_SECONDS" in _wsrc and "rest_sweep(" in _wsrc)
    check("gateway failure falls back to per-tick REST, it is never fatal",
          "REST polling only" in _wsrc and "RECONCILE_SECONDS if live else 0" in _wsrc)
    check("the gateway can be switched off from .env", "DISCORD_GATEWAY" in _wsrc)
    check("a rescued message is logged (frequent means gateway.py has a bug)",
          "gateway missed" in _wsrc)
    # Observed live 2026-08-01: the first sweep after a restart reported the
    # backlog it was SUPPOSED to catch up as a gateway miss. A warning that
    # fires when nothing is wrong stops being read.
    check("the miss warning needs an unbroken socket across the whole window",
          "prev_sweep_live" in _wsrc and "gw.reconnects == prev_reconnects" in _wsrc)
    wd.write_beacon(9, gateway=True)
    _b = json.loads((wd.WD_STATE / "beacon.json").read_text(encoding="utf-8"))
    check("beacon records which transport is carrying us", _b.get("gateway") is True)

    # ---------------------------------------------------------------- desktop
    # Pure logic only - no window is opened and no key is synthesised here. The
    # GUI half was verified by hand against a real desktop; what the suite pins
    # is the SAFETY SHAPE, which is the part that must not drift.
    print("== desktop verbs ==")
    import argparse as _ap
    sys.path.insert(0, str(HERE.parent / "desktop"))
    import desktop as dt
    dt.LOGS = SAND / "logs"

    check("the registry is closed and named (no click/run/exec verb)",
          set(dt.VERBS) == {"windows", "screenshot", "focus", "open", "key",
                            "type-into", "close"})
    check("no verb takes raw coordinates",
          not any(k in dt.VERBS for k in ("click", "move", "drag", "run", "exec", "shell")))
    # The two verbs that inject input are the highest-risk AND the only ones whose
    # effect the module cannot confirm - measured 2026-08-01, SendInput reported
    # success while the app did nothing. That combination stays off the phone.
    check("key/type-into are not reachable from Discord",
          "key" not in dt.REMOTE_VERBS and "type-into" not in dt.REMOTE_VERBS)
    check("the read verbs are reachable from Discord",
          {"screenshot", "windows"} <= set(dt.REMOTE_VERBS))
    check("input verbs refuse to claim success they cannot verify",
          "NOT verified" in dt.UNVERIFIED and "screenshot" in dt.UNVERIFIED)

    check("`open` only launches names from the allowlist",
          dt.run("open", _ap.Namespace(target="calc.exe && del /f *", text=None,
                                       window=None, out=None, json=False,
                                       caller="t"))[0] is False)
    check("`open` names a real app as allowed",
          "notepad" in dt.APPS and all(isinstance(v, list) for v in dt.APPS.values()))
    check("key combos are validated against a fixed table",
          "unknown key" in dt.run("key", _ap.Namespace(
              target="x", text="ctrl+launch_missile", window=None, out=None,
              json=False, caller="t"))[1])

    _real_lw = dt.list_windows
    dt.list_windows = lambda: [{"hwnd": 1, "title": "Notes - a.txt", "pid": 10},
                               {"hwnd": 2, "title": "Notes - b.txt", "pid": 11},
                               {"hwnd": 3, "title": "Solo", "pid": 12}]
    check("an ambiguous window is an error, never a guess",
          _raises(dt.find_window, "notes"))
    check("an unambiguous window resolves", dt.find_window("solo")["hwnd"] == 3)
    check("an exact title beats a substring",
          dt.find_window("Notes - a.txt")["hwnd"] == 1)
    check("a window that does not exist says so", _raises(dt.find_window, "nope"))
    dt.list_windows = _real_lw

    # Secrets rule: a typed password must not end up in a log file that is not
    # treated as a secret. The audit records the LENGTH, never the text.
    dt.run("type-into", _ap.Namespace(target="nothing-matches-this", text="hunter2",
                                      window=None, out=None, json=False, caller="t"))
    _audit = (SAND / "logs" / "desktop.log").read_text(encoding="utf-8")
    check("every invocation is audited", '"verb": "type-into"' in _audit)
    check("the audit records the caller", '"caller": "t"' in _audit)
    check("typed text is NEVER written to the log (it could be a password)",
          "hunter2" not in _audit and "7 chars" in _audit)
    check("run() reports failure instead of raising at a chat handler",
          dt.run("nope", _ap.Namespace(target=None, text=None, window=None, out=None,
                                       json=False, caller="t"))[0] is False)

    _wsrc2 = (HERE / "watchdog.py").read_text(encoding="utf-8")
    check("!screen and !desktop are control commands (work when every desk is dead)",
          "!screen" in wd.CONTROL_COMMANDS and "!desktop" in wd.CONTROL_COMMANDS)
    check("the watchdog only ever calls REMOTE_VERBS", "dt.REMOTE_VERBS" in _wsrc2)
    check("tools\\desktop is imported lazily so its absence cannot stop the bus",
          "import desktop as dt" in _wsrc2.split("def run_desktop_verb")[1][:800]
          and "\nimport desktop" not in _wsrc2)
    check("the screenshot-cannot-be-redacted risk is stated where it is used",
          "CANNOT BE REDACTED" in _wsrc2)

    # ----------------------------------------------------- orchestrator verbs
    print("== orchestrator verbs ==")
    sys.path.insert(0, str(HERE.parent / "orchestrator"))
    import fleet_ops as fo
    _real_projects, _real_tmpl = fo.PROJECTS, fo.TEMPLATE
    # Align with api.ROOT: disk_projects() reads components through
    # api.project_components, which resolves under api.ROOT\projects.
    fo.PROJECTS = SAND / "projects"
    fo.ARCHIVE = fo.PROJECTS / "_archive"
    fo.PROJECTS.mkdir(parents=True, exist_ok=True)

    # Names become a folder, a git repo, a Discord category AND a session id.
    for bad in ("Recipe App", "recipe_app", "Recipe-App", "recipe--app", "-recipe", ""):
        check(f"rejects the non-kebab name {bad!r}", _raises(fo.check_name, bad))
    check("accepts a kebab-case name", fo.check_name("recipe-app") == "recipe-app")

    # A new project gets a folder and channels, NOT a repo. Owner, 2026-08-04:
    # "maybe it is just a temp project" - he tells the desk when to make one.
    import inspect as _insp
    check("new-project does NOT create a git repo unless asked",
          _insp.signature(fo.new_project).parameters["git"].default is False)
    _out = fo.new_project("recipe-app", ["app", "backend"], "a cookbook",
                          discord=False)
    check("...so a default stamp leaves no .git behind",
          not (fo.PROJECTS / "recipe-app" / ".git").exists())
    _p = fo.PROJECTS / "recipe-app"
    check("new-project stamps the template", (_p / "CLAUDE.md").is_file())
    check("new-project creates each component folder",
          (_p / "app").is_dir() and (_p / "backend").is_dir())
    # An empty folder is not tracked by git, so a fresh clone would come back
    # missing the desk entirely.
    check("component folders survive a git clone (.gitkeep)",
          (_p / "app" / ".gitkeep").is_file())
    check("new-project seeds memory\\sessions", (_p / "memory" / "sessions").is_dir())
    # The SEED FILES, not just the folder. The bare `memory/` ignore pattern
    # matches at every depth and silently dropped these from the public tree -
    # so a stamped project was born with an empty memory while its CLAUDE.md
    # said "copy _template.md" (found 2026-08-15, diffing the live instance
    # against the repo). The folder check above stayed green throughout,
    # because fleet_ops mkdirs it; only the files can prove the template ships.
    check("...including the memory index the template promises",
          (_p / "memory" / "MEMORY.md").is_file())
    check("...and the session-notes template desks are told to copy",
          (_p / "memory" / "sessions" / "_template.md").is_file())
    check("...which the TEMPLATE itself tracks, so a clone cannot lose them again",
          (real_root / "templates" / "project" / "memory" / "MEMORY.md").is_file()
          and "!templates/project/memory/" in
          (real_root / ".gitignore").read_text(encoding="utf-8"))
    _claude = (_p / "CLAUDE.md").read_text(encoding="utf-8")
    check("placeholders are filled", "{{" not in _claude and "recipe-app" in _claude)
    check("the description lands in the project constitution", "a cookbook" in _claude)
    # The template ships ONE component row; a two-desk project must not document one.
    check("the components table gets a row per component",
          "| app\\ |" in _claude and "| backend\\ |" in _claude)

    # Idempotency is a hard requirement: these run from a phone, where a dropped
    # reply looks exactly like a failure and the user just says it again.
    (_p / "CLAUDE.md").write_text(_claude + "\nEDITED BY THE PROJECT\n", encoding="utf-8")
    _again = fo.new_project("recipe-app", ["app", "backend"], "a cookbook",
                            discord=False, git=False)
    check("re-running new-project creates nothing new", _again["created"] == []
          and _again["existed"] is True)
    check("re-running never clobbers an edited CLAUDE.md",
          "EDITED BY THE PROJECT" in (_p / "CLAUDE.md").read_text(encoding="utf-8"))
    check("a reserved folder name is refused as a component",
          _raises(fo.new_project, "other-app", ["memory"]))

    # status: the point of the verb is telling a live desk from litter (R2).
    fo.wd, fo.api = wd, api
    wd.RUNS.mkdir(parents=True, exist_ok=True)
    for f in wd.RUNS.glob("*.json"):
        f.unlink()
    claim("live-desk", pid=_os.getpid(), machine=api.MACHINE)
    claim("dead-desk", pid=99999997, machine=api.MACHINE)
    claim("other-pc", pid=_os.getpid(), machine="SOME-OTHER-PC")
    wd.write_json_atomic(wd.RUNS / "run-desk.json",
                         {"session": "run-desk", "pid": _os.getpid()})
    check("status: live terminal pid -> live", fo.claim_state("live-desk")[0] == "live")
    check("status: an active headless run -> working (no claim needed)",
          fo.claim_state("run-desk")[0] == "working")
    check("status: dead pid -> stale", fo.claim_state("dead-desk")[0] == "stale")
    # Normal after the workspace moves to a new PC - must not read as live.
    check("status: another machine's claim -> stale",
          fo.claim_state("other-pc")[0] == "stale")
    _st = fo.status(prune=True)
    _by = {d["session"]: d for d in _st["desks"]}
    check("prune removes the stale claims", _by["dead-desk"]["pruned"]
          and _by["other-pc"]["pruned"])
    check("prune leaves the live desk alone", not _by["live-desk"]["pruned"]
          and (wd.SESSIONS / "live-desk.json").is_file())
    check("a run-only desk appears in status without any claim file",
          "run-desk" in _by)
    (wd.RUNS / "run-desk.json").unlink(missing_ok=True)

    # tab_title travelled with the visible-terminal verb into fleet_ops.
    check("tab_title: orchestrator", fo.tab_title("orchestrator") == "🎛 Omnius")
    check("tab_title: project component",
          fo.tab_title("demo-proj.democomp") == "📁 demo-proj · democomp")
    check("tab_title: component name containing a dot survives",
          fo.tab_title("proj.a.b") == "📁 proj · a.b")
    _ascii_t = fo.tab_title("demo-proj.democomp", ascii_only=True)
    check("tab_title: ascii_only strips emoji but keeps the identifying text",
          _ascii_t.isascii() and "demo-proj" in _ascii_t and "democomp" in _ascii_t)

    _skills = HERE.parent.parent / ".claude" / "skills"
    for _v in ("new-project", "spawn-session", "status", "archive-project"):
        check(f"/{_v} skill exists", (_skills / _v / "SKILL.md").is_file())
        _txt = (_skills / _v / "SKILL.md").read_text(encoding="utf-8")
        check(f"/{_v} has frontmatter a session can match on",
              _txt.startswith("---") and f"name: {_v}" in _txt)
        check(f"/{_v} drives the script, not improvised commands",
              "fleet_ops.py" in _txt)
    # Destructive fleet operations confirm with the user first (root CLAUDE.md §3).
    # Matched on words, not on a phrase: the sentence wraps in the source.
    _arch = " ".join((_skills / "archive-project" / "SKILL.md")
                     .read_text(encoding="utf-8").split())
    check("/archive-project confirms before destroying",
          "This is destructive. Confirm with the user first" in _arch)
    check("/archive-project lets desks write memory before they are killed",
          "memory" in _arch and "before killing anything" in _arch.lower())
    check("the verbs write through to orchestrator status",
          all("status.md" in (_skills / v / "SKILL.md").read_text(encoding="utf-8")
              for v in ("new-project", "spawn-session", "archive-project")))
    fo.PROJECTS, fo.TEMPLATE = _real_projects, _real_tmpl

    # -------------------------------------------------------------- heartbeat
    print("== heartbeat ==")
    from datetime import datetime as _dt
    for f in wd.SESSIONS.glob("*.json"):
        f.unlink()
    _hb_state = wd.WD_STATE / "heartbeat.json"
    _hb_state.unlink(missing_ok=True)

    # THE property the whole design turns on: a quiet moment must cost nothing.
    # Waking an Opus session every 30 min just to conclude "nothing to say" is
    # ~48 sessions a day, against goal 6 (nothing spends unless there is work).
    # lastBackupNag is part of "nothing to do": on an instance with no backup
    # folder set - every fresh one - the daily nag is a legitimate reason, and
    # leaving it out of the fixture tested the machine rather than the rule.
    _quiet = {"lastDaily": "2026-08-01", "lastBackupNag": "2026-08-01"}
    check("nothing to do -> no reason to wake anyone",
          wd.heartbeat_reasons(dict(_quiet), _dt(2026, 8, 1, 6, 0)) == [])
    check("before 07:00 the daily briefing is not due",
          not any("daily" in r for r in
                  wd.heartbeat_reasons({}, _dt(2026, 8, 1, 6, 59))))
    check("after 07:00 the daily briefing is due once",
          any("daily" in r for r in wd.heartbeat_reasons({}, _dt(2026, 8, 1, 7, 1))))
    # A machine switched on at 18:00 gets today's briefing once, not eleven.
    check("a briefing already sent today does not fire again",
          not any("daily" in r for r in wd.heartbeat_reasons(
              {"lastDaily": "2026-08-01"}, _dt(2026, 8, 1, 18, 0))))
    check("gardening is due on Monday",
          any("weekly" in r for r in wd.heartbeat_reasons(
              {"lastDaily": "2026-08-03"}, _dt(2026, 8, 3, 8, 0))))
    check("gardening is not due on a Tuesday",
          not any("weekly" in r for r in wd.heartbeat_reasons(
              {"lastDaily": "2026-08-04"}, _dt(2026, 8, 4, 8, 0))))
    claim("ghost", pid=99999997, watcherPid=99999996, machine=api.MACHINE)
    check("a stale claim is a reason to wake",
          any("stale" in r for r in wd.heartbeat_reasons(
              {"lastDaily": "2026-08-01"}, _dt(2026, 8, 1, 6, 0))))
    # Found on the FIRST heartbeat ever fired (2026-08-01): the envelope said
    # "prune orchestrator", the watchdog then spawned the orchestrator to handle
    # it, and by the time Omnius read the list it WAS that orchestrator - alive.
    # Acting on the snapshot would have deleted its own claim and invited a
    # second orchestrator onto the desk (the R3 duplicate-desk failure).
    for f in wd.SESSIONS.glob("*.json"):
        f.unlink()
    claim("orchestrator", pid=99999995, watcherPid=99999994, machine=api.MACHINE)
    check("the orchestrator's OWN stale claim never wakes it (it would prune itself)",
          wd.heartbeat_reasons(dict(_quiet), _dt(2026, 8, 1, 6, 0)) == [])
    claim("ghost", pid=99999997, watcherPid=99999996, machine=api.MACHINE)
    check("...but another desk's stale claim still does",
          any("ghost" in r for r in wd.heartbeat_reasons(
              dict(_quiet), _dt(2026, 8, 1, 6, 0))))
    (wd.SESSIONS / "orchestrator.json").unlink(missing_ok=True)
    claim("foreign", pid=_os.getpid(), watcherPid=_os.getpid(), machine="OTHER-PC")
    check("another machine's claim counts as stale (normal after a move)",
          "foreign" in wd.stale_claims())
    claim("real", pid=_os.getpid(), watcherPid=_os.getpid(), machine=api.MACHINE)
    check("a live desk is never called stale", "real" not in wd.stale_claims())

    _spawns = []
    _real_spawn2, wd.start_run = wd.start_run, lambda s, **k: _spawns.append(s)
    _hb_box = wd.INBOX / "orchestrator"
    shutil.rmtree(_hb_box, ignore_errors=True)
    wd.fire_heartbeat()
    _envs = sorted(_hb_box.glob("heartbeat-*.json")) if _hb_box.is_dir() else []
    check("a heartbeat with reasons writes ONE envelope", len(_envs) == 1)
    _env = json.loads(_envs[0].read_text(encoding="utf-8"))
    check("the envelope is marked from:heartbeat, not from a person",
          _env["from"] == "heartbeat")
    check("the envelope names what the watchdog noticed", "stale claim" in _env["text"])
    check("the envelope repeats the quiet rule to the agent",
          "END THE TURN SILENTLY" in _env["text"])
    # The list is composed at write time and read minutes later, after a spawn.
    check("the envelope presents its list as a snapshot to re-check, not a work order",
          "snapshot, not a work order" in _env["text"]
          and "RE-CHECK EACH ITEM BEFORE ACTING" in _env["text"])
    check("it points at the checklist, not at a hardcoded task list",
          "HEARTBEAT.md" in _env["text"])
    # Firing again immediately would spawn a second orchestrator on the same desk.
    wd.fire_heartbeat()
    check("a second call inside the interval does nothing",
          len(sorted(_hb_box.glob("heartbeat-*.json"))) == 1)

    # With no reasons, the check must stamp itself and stay completely silent.
    for f in wd.SESSIONS.glob("*.json"):
        f.unlink()
    shutil.rmtree(_hb_box, ignore_errors=True)
    _st_hb = wd.read_heartbeat_state()
    _st_hb["lastCheck"] = "2000-01-01T00:00:00"
    _st_hb["lastDaily"] = _dt.now().strftime("%Y-%m-%d")
    _st_hb["lastWeekly"] = _dt.now().strftime("%Y-%m-%d")
    wd.write_json_atomic(_hb_state, _st_hb)
    _spawns.clear()
    wd.fire_heartbeat()
    check("a quiet heartbeat writes no envelope",
          not _hb_box.is_dir() or not list(_hb_box.glob("heartbeat-*.json")))
    check("a quiet heartbeat spawns NOBODY (this is the whole cost argument)",
          _spawns == [])
    check("a quiet heartbeat still stamps the check, so it does not retry every pass",
          wd.read_heartbeat_state().get("lastCheck") != "2000-01-01T00:00:00")
    wd.start_run = _real_spawn2

    check("HEARTBEAT_MINUTES=0 switches the whole thing off",
          (lambda: (api.ENV.__setitem__("HEARTBEAT_MINUTES", "0"),
                    wd._heartbeat_minutes() == 0)[1])())
    # A wrong-but-non-empty .env value has killed this service before.
    api.ENV["HEARTBEAT_MINUTES"] = "half an hour"
    check("a garbage HEARTBEAT_MINUTES falls back instead of crashing the bus",
          wd._heartbeat_minutes() == 30)
    api.ENV.pop("HEARTBEAT_MINUTES", None)
    check("the default is 30 minutes with no .env at all", wd._heartbeat_minutes() == 30)

    _sk = (HERE.parent.parent / ".claude" / "skills" / "omnius" / "SKILL.md").read_text(encoding="utf-8")
    check("/omnius teaches sessions what a heartbeat envelope is",
          'from: "heartbeat"' in _sk)
    # Whitespace-normalised: these sentences wrap in the source, and matching a
    # raw phrase across a line break has now failed twice in this suite.
    check("/omnius exempts the heartbeat from the acknowledge-first rule",
          "acknowledge-first rule does **not** apply to heartbeats" in " ".join(_sk.split()))
    _hbmd = _mem_file("orchestrator/HEARTBEAT.md").read_text(encoding="utf-8")
    check("HEARTBEAT.md is no longer marked as unbuilt", "Phase 4)" not in _hbmd
          and "Built 2026-08-01" in _hbmd)

    # Discord renders no markdown tables: a reply that looks right in the editor
    # arrives as a wall of literal pipes on his phone (#transcribe, 2026-08-06).
    # Both files, because a desk reads USER.md even when it never runs /omnius.
    _skflat = " ".join(_sk.split())
    check("/omnius warns that Discord renders no markdown tables",
          "renders NO markdown tables" in _skflat)
    check("...and names the replacement, so it is a rule not a complaint",
          "fenced code block" in _skflat and "bullets" in _skflat)
    _usr = " ".join(_mem_file("shared/USER.md").read_text(encoding="utf-8").split())
    check("USER.md carries the no-tables rule for desks that skip the skill",
          "Never send a markdown table to Discord" in _usr)

    # The deny-lists were emptied to .env on 2026-08-06 ("no allow questions, no
    # matter where"). That is only safe if the judgment replacing them is
    # WRITTEN DOWN - otherwise removing the fence is a pure loss. These checks
    # exist so the brake cannot be quietly dropped in a later edit.
    check("USER.md states that the model is the brake now the deny-lists are gone",
          "YOU are the brake now" in _usr)
    # The enumeration lives in the skill, not USER.md: USER.md is on the
    # always-read path and has a hard budget, so the compact rule goes there and
    # the list where it costs nothing.
    check("...and the skill enumerates what counts, not just 'be careful'",
          all(t in _skflat for t in ("force-push", "irreversible", "killing processes")))
    check("...and says routine work must NOT ask (the friction he removed)",
          "Routine work never asks" in _usr and "routine work never asks" in _skflat)
    check("/omnius carries the same brake for desks handling mail",
          "The allow-list is no longer the safety" in _skflat)
    for _sf2 in (".claude/settings.json", "daybook/.claude/settings.json",
                 "tools/fleet/.claude/settings.json",
                 "tools/transcribe/.claude/settings.json",
                 "templates/project/.claude/settings.json"):
        _pp = json.loads((HERE.parent.parent / _sf2).read_text(encoding="utf-8"))["permissions"]
        check(f"{_sf2}: auto-allows shell (no prompt on a screen nobody watches)",
              "Bash" in _pp["allow"] and "PowerShell" in _pp["allow"])
        check(f"{_sf2}: still refuses to read .env (the one fence he kept)",
              any("env" in x.lower() for x in _pp.get("deny", [])))

    print(f"\n==== {passed} passed, {failed} failed ====")
finally:
    shutil.rmtree(SAND, ignore_errors=True)

sys.exit(1 if failed else 0)
