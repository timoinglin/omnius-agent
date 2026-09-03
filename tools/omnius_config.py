"""Omnius configuration — the single reader for `config\\*.ini` and root `.env`.

Named `omnius_config`, not `config`: a bare `config` module would shadow, and be
shadowed by, `.claude\\skills\\watch\\scripts\\config.py` as soon as both land on
sys.path. Import collisions of that kind fail a long way from their cause.

Why this exists
---------------
Settings used to live in four places and three formats: root `.env` (secrets
*and* non-secrets), root `fleet.json`, `daybook\\config.ini`, and seven
`.claude\\settings.json`. There was no single place to look, and every new
capability added another. Owner, 2026-08-05: "we need to make a propper config
file".

The rules this module enforces
------------------------------
1. **Precedence is env var > config file > built-in default.** A one-off
   `set NOTES_HOST=0.0.0.0 && python app.py` must still win over the file,
   without editing anything. (The shape `daybook\\app.py` already used.)
2. **Secrets never live in `config\\`.** A config value names the `.env` key
   that holds the credential — `password_env = EMAIL_WORK_PASSWORD` — and this
   module resolves it. That indirection is what lets `config\\` be readable,
   diffable and portable while credentials stay in one gitignored file
   (CLAUDE.md par.5).
3. **Nothing here may raise.** Config is the one thing that can take the whole
   fleet down: it is read at boot by the watchdog, every desk and the notes
   server. A corrupt or missing file falls back to defaults and is *reported*,
   never fatal — the rule `fleet.json` already follows ("broken fleet.json must
   never stop a spawn", watchdog.py). Every public function in this module
   returns a usable value for any input, including no `config\\` folder at all.

The SPEC table at the bottom drives validation and the `!config` dump together,
so a setting cannot exist in one and be missing from the other.
"""
import configparser
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("OMNIUS_CONFIG_DIR") or (ROOT / "config"))

_problems = []          # accumulated while reading; surfaced by problems()


def _note(msg):
    if msg not in _problems:
        _problems.append(msg)


# --- root .env ----------------------------------------------------------------
#
# THE canonical .env parser. `tools\discord\api.py` delegates to it: one parser,
# not two that drift (topics\lessons.md - "api.py is the single parser"). It
# lives here rather than there because config\ needs it for secret indirection
# and must not depend on the Discord layer.

def read_env_text(p):
    """Decode a .env written by any Windows tool. PowerShell 5.1's `>` and
    Out-File default to UTF-16, and Notepad may add a UTF-8 BOM - blind utf-8
    turns either into mojibake and the keys silently vanish."""
    raw = p.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def load_env(root=None):
    """Parse root .env. Tolerates what people actually write: quoted values,
    inline comments, `export KEY=`, mixed-case keys, CRLF/LF/CR. Keys are
    upper-cased so lookups never miss on case alone."""
    env = {}
    p = (Path(root) if root else ROOT) / ".env"
    try:
        if not p.exists():
            return env
        text = read_env_text(p)
    except OSError as e:
        _note(f".env unreadable ({e}) - continuing without it")
        return env
    # Split on ASCII line breaks ONLY - str.splitlines() also breaks on U+2028
    # and U+0085, which can appear inside a pasted value and truncate a token.
    for line in re.split(r"\r\n|\r|\n", text):
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


def env_value(name, env=None):
    """A value from the process environment first, then root .env. -> str."""
    if not name:
        return ""
    live = os.environ.get(name)
    if live:
        return live.strip()
    return ((env if env is not None else load_env()).get(name.upper(), "")).strip()


# --- config\<name>.ini --------------------------------------------------------

def ini_path(name):
    return CONFIG_DIR / f"{name}.ini"


def load(name, legacy=None):
    """-> {section: {key: value}} for config\\<name>.ini. Never raises.

    `legacy` is an optional older path to fall back to, so a half-migrated tree
    still boots (daybook\\config.ini while config\\notes.ini is being adopted).
    A missing file is the NORMAL case - every key has a default.
    """
    for p in (ini_path(name), Path(legacy) if legacy else None):
        if p is None:
            continue
        parser = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
        try:
            # utf-8-SIG, not utf-8: a leading BOM makes configparser reject the
            # whole file with MissingSectionHeaderError - every setting silently
            # reverts to its default. And BOMs arrive by accident constantly on
            # Windows: Notepad writes one, and PowerShell 5.1's
            # `Set-Content -Encoding utf8` has no no-BOM option at all, so our
            # own installer produced one (found 2026-08-11 before it shipped).
            # utf-8-sig reads files with or without it.
            with open(p, encoding="utf-8-sig") as fh:
                parser.read_file(fh)
        except configparser.DuplicateSectionError as e:
            # ONE duplicate heading must not cost every setting in the file.
            # Strict parsing raises, load() falls back to built-in defaults, and
            # a config that is 99% correct behaves as if it were absent - which
            # is how an installed backup folder reported itself unconfigured
            # (2026-08-11). Re-read leniently (later keys win, as a human would
            # expect) and SAY SO, so it gets fixed rather than lived with.
            _problems.append(f"config\\{p.name}: {e.args[0].splitlines()[0]} "
                             f"- read leniently, later values win; merge the sections")
            lenient = configparser.ConfigParser(inline_comment_prefixes=("#", ";"),
                                                strict=False)
            try:
                with open(p, encoding="utf-8-sig") as fh:
                    lenient.read_file(fh)
                # Same shape every caller expects - a plain dict of dicts, not
                # a parser. Returning the parser here type-errored in get().
                return {s: dict(lenient[s]) for s in lenient.sections()}
            except (OSError, UnicodeDecodeError, configparser.Error):
                continue
        except FileNotFoundError:
            continue
        except (OSError, UnicodeDecodeError, configparser.Error) as e:
            # Reported, not fatal. A typo in a config file must never stand
            # between him and a working fleet.
            _note(f"{p.name} ignored ({type(e).__name__}: {e}) - using defaults")
            continue
        return {s: _unfold(dict(parser[s]), p.name, s) for s in parser.sections()}
    return {}


def _unfold(section, filename, section_name):
    """Repair a setting that an indented line swallowed. -> {key: value}.

    In INI, a line that starts with whitespace CONTINUES the previous value. So

        visibility = own
         media_out = 0        <- one stray space

    is not two settings, it is `visibility = "own\\nmedia_out = 0"` - and since
    an unknown visibility is refused (rightly), one invisible space silently
    unlisted an invited person and left the bridge idling. That happened on
    2026-08-19, live, and the only clue was a value with a newline in it.

    Nothing in config\\ is ever meant to be multi-line, so a continuation that
    looks like `key = value` is put back where it belongs and REPORTED. Anything
    else is left exactly as written - guessing beyond this would be its own trap.
    """
    out = {}
    for key, value in section.items():
        if "\n" not in (value or ""):
            out[key] = value
            continue
        first, rest = value.split("\n", 1)
        out[key] = first
        for line in rest.split("\n"):
            m = re.match(r"\s*([A-Za-z0-9_.-]+)\s*[=:]\s*(.*)$", line)
            if m:
                out[m.group(1).strip().lower()] = m.group(2).strip()
                _note(f"config\\{filename} [{section_name}]: '{m.group(1).strip()}' "
                      f"was indented, which INI reads as part of '{key}' above it "
                      f"- read as a setting of its own; remove the leading space")
            else:
                out[key] = out[key] + "\n" + line
    return out


def get(cfg, section, key, env_var=None, default="", env=None):
    """env var > config file > default, as a stripped string.

    Pass `env` to reuse an already-parsed .env (the watchdog holds one as
    api.ENV). Without it every lookup re-reads and re-parses the file, which is
    both wasteful inside a 3-second poll loop and invisible to anything that
    patches that dict rather than the disk."""
    live = env_value(env_var, env) if env_var else ""
    if live:
        return live
    return str((cfg.get(section) or {}).get(key) or default).strip()


def get_int(cfg, section, key, env_var=None, default=0, env=None):
    raw = get(cfg, section, key, env_var, "", env)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        _note(f"[{section}] {key}={raw!r} is not a number - using {default}")
        return default


TRUE = ("1", "true", "yes", "on")
FALSE = ("0", "false", "no", "off")


def get_bool(cfg, section, key, env_var=None, default=False, env=None):
    raw = get(cfg, section, key, env_var, "", env).lower()
    if not raw:
        return default
    if raw in TRUE:
        return True
    if raw in FALSE:
        return False
    _note(f"[{section}] {key}={raw!r} is not true/false - using {default}")
    return default


def group(cfg, prefix):
    """-> {label: {key: value}} for every `[prefix.label]` section.

    The multi-account shape: `[account.work]`, `[account.personal]`. Used by
    email (several accounts, owner 2026-08-05) and available to anything else
    that needs N of a thing without a code change.
    """
    out = {}
    for name, body in cfg.items():
        head, dot, label = name.partition(".")
        if dot and head.strip().lower() == prefix.lower() and label.strip():
            out[label.strip()] = body
    return out


def secret(body, key="password_env", env=None):
    """Resolve a `*_env` indirection. -> (value, env_key_name).

    The value is returned for USE, never for display: `describe()` and the
    `!config` verb only ever report whether it is set. Nothing in this module
    prints a credential.
    """
    env_key = str((body or {}).get(key) or "").strip()
    if not env_key:
        return "", ""
    return env_value(env_key, env), env_key


# --- what exists, and what it should be ---------------------------------------
#
# One table so a setting cannot be validated in one place and missing from the
# dump in another. (name, section, key, env_var, default, kind)

SPEC = [
    ("omnius", "omnius", "name", "OMNIUS_NAME", "Omnius", "str"),
    ("omnius", "omnius", "machine_name", "MACHINE_NAME", "", "str"),
    ("omnius", "omnius", "language", "OMNIUS_LANGUAGE", "", "str"),
    ("omnius", "omnius", "heartbeat_minutes", "HEARTBEAT_MINUTES", "30", "int"),
    ("omnius", "omnius", "gateway", "DISCORD_GATEWAY", "1", "bool"),
    ("omnius", "backup", "folder", "OMNIUS_BACKUP_DIR", "", "str"),
    ("omnius", "browser", "device_id", "OMNIUS_BROWSER_DEVICE_ID", "", "str"),
    # The bus talking about itself. working_notice_minutes = how often the
    # watchdog says "still working" while a desk is provably working with his
    # mail queued behind it; 0 turns the notices off entirely.
    ("omnius", "bus", "working_notice_minutes", "OMNIUS_WORKING_NOTICE_MINUTES", "3", "int"),
    # Desk mail (docs\DELEGATION.md). hop_ttl = forward hops a delegation chain
    # may spend (replies are free); loop_budget = scheduled continuation runs
    # before a loop must checkpoint with the owner (consumed by schedule.py in
    # Phase D); cross_project_requires_ok = hold cross-project desk mail for an
    # ok in Discord - OFF by default since 2026-09-03, because it contradicted
    # his older and broader rule (auto-allow everything, the model is the
    # brake) and asked him to approve his own fleet's routine traffic. Set it
    # to 1 to gate.
    ("omnius", "delegation", "hop_ttl", "OMNIUS_HOP_TTL", "3", "int"),
    ("omnius", "delegation", "loop_budget", "OMNIUS_LOOP_BUDGET", "5", "int"),
    ("omnius", "delegation", "cross_project_requires_ok", "OMNIUS_CROSS_PROJECT_OK", "0", "bool"),
    # Workflows (docs\DELEGATION.md D11): a chain that must keep running across
    # desks and runs. The budget is per WORKFLOW, not per hop - inside an open
    # one the depth cap stops biting, and these three are what end it instead.
    ("omnius", "delegation", "workflow_budget", "OMNIUS_WORKFLOW_BUDGET", "25", "int"),
    ("omnius", "delegation", "workflow_deadline_hours", "OMNIUS_WORKFLOW_DEADLINE_HOURS", "24", "int"),
    ("omnius", "delegation", "workflow_stall_hours", "OMNIUS_WORKFLOW_STALL_HOURS", "3", "int"),
    # Does THIS instance own the public repo? Off by default, because almost
    # every instance is somebody's install rather than the source of the
    # project. It changes one thing only: whether a desk may push. Committing
    # is right on every instance - !update rebases local commits onto each new
    # release, so local work survives updates instead of blocking them.
    ("omnius", "fleet", "maintainer", "OMNIUS_MAINTAINER", "0", "bool"),
    ("notes", "notes", "notes_dir", "NOTES_DIR", "notes", "str"),
    ("notes", "notes", "host", "NOTES_HOST", "127.0.0.1", "str"),
    ("notes", "notes", "port", "PORT", "5111", "int"),
    ("email", "email", "default", None, "", "str"),
    ("email", "email", "max_fetch", None, "25", "int"),
]

# Credentials, by the .env key that must hold them. Reported as set/NOT SET.
SECRET_KEYS = ("DISCORD_BOT_TOKEN", "DISCORD_GUILD_ID", "DISCORD_OWNER_ID")

# Optional capabilities: [section] in config\ai.ini -> what it does.
# Each is OFF until it has BOTH a provider and a key that is actually set.
CAPABILITIES = (("image", "image generation"),
                ("video", "video generation"),
                ("tts", "text to speech"),
                ("documents", "document/OCR extraction"))


def capability_status(env=None):
    """-> list of (name, provider, env_key, state) for every optional capability.

    state is 'off' (no provider chosen), 'ready', or 'NO KEY'. The rule this
    enforces is roadmap.md's: absence disables ONE capability with a clear
    message and never breaks anything else, so a machine with none of these
    keys runs exactly as well as one with all of them.

    'windows' is a real provider that needs no key - the built-in SAPI5 voices
    - so it must not be reported as missing one.
    """
    env = load_env() if env is None else env
    cfg = load("ai")
    out = []
    for name, _what in CAPABILITIES:
        body = cfg.get(name) or {}
        provider = str(body.get("provider") or "").strip()
        env_key = str(body.get("api_key_env") or "").strip()
        if not provider:
            out.append((name, "", "", "off"))
        elif provider.lower() == "windows":
            out.append((name, provider, "", "ready"))
        elif not env_key:
            out.append((name, provider, "", "NO KEY"))
        else:
            out.append((name, provider, env_key,
                        "ready" if env_value(env_key, env) else "NO KEY"))
    return out


CREDENTIAL_KEYS = ("password_env", "token_env")


def credential_key(body):
    """-> the config key naming this account's credential, or "".

    Different providers keep different credentials: an IMAP mailbox has a
    password, an OAuth one has a refresh token. Both are named here and stored
    in .env, so the reporting has to look for whichever the account uses rather
    than assuming a password exists.
    """
    for key in CREDENTIAL_KEYS:
        if str((body or {}).get(key) or "").strip():
            return key
    return ""


def account_status(name="email", prefix="account", env=None):
    """-> list of (label, user, env_key, 'set'|'NOT SET'|'no password_env').

    Per-account credentials cannot live in SECRET_KEYS: that tuple is fixed at
    import time and these are DISCOVERED from config - the whole point of the
    multi-account shape is that he adds a mailbox without anyone changing code.
    So they get their own reporter, and `describe()` calls both.

    Without this, `!config` would print nothing at all about mail while
    config\\README.md promises it prints everything - and the first thing he
    would reach for when authentication fails is exactly this answer.
    Values are never returned; only whether the named key has one.
    """
    env = load_env() if env is None else env
    out = []
    for label, body in sorted(group(load(name), prefix).items()):
        key = credential_key(body)
        env_key = str((body or {}).get(key) or "").strip() if key else ""
        user = str((body or {}).get("user") or "").strip()
        if not env_key:
            out.append((label, user, "", "no password_env"))
        else:
            out.append((label, user, env_key,
                        "set" if env_value(env_key, env) else "NOT SET"))
    return out


# --- guests: people who are not the owner and may still use the bus -----------

# `from` values the fleet itself uses. A guest may not wear one of these, and
# anything NOT in here is a person (see watchdog.is_human_sender).
RESERVED_SENDERS = ("owner", "omnius", "heartbeat", "schedule")
_SNOWFLAKE = re.compile(r"^\d{17,20}$")


def guests():
    """-> {label: {"id", "name", "channels": [str], "scope"}} per `[guest.<label>]`.

    A guest is someone who is NOT the owner and may still write into ONE desk's
    channels. Added 2026-08-12 so a project's real-world owner (the artist whose
    brand one project is building) could send their own ideas and images into
    that project's channel without being able to reach the rest of the fleet. The
    `[guest.<label>]` shape is `group()`'s, the same one mail accounts use, so a
    second guest needs no code change.

    FAILS CLOSED, four ways, because this is an authorisation list and a
    generous reading of a typo is how one becomes a hole:

    - no `channels` -> the entry is ignored. It is never read as "all of them".
    - a `user_id` that is not a snowflake -> ignored, so a half-pasted id cannot
      widen into a wildcard.
    - a label that collides with a system sender -> ignored, so no guest can
      arrive wearing the fleet's own `from` value and be mistaken for it.
    - anything unreadable -> {} (rule 3 of this module: config never raises).

    Every rejection is *reported* through problems() rather than swallowed. A
    guest who silently cannot write looks exactly like the bug this feature was
    built to remove, and it took reading the watchdog's source to find it once
    already.
    """
    out = {}
    for label, body in sorted(group(load("guests"), "guest").items()):
        key = label.strip().lower()
        body = body or {}
        uid = str(body.get("user_id") or "").strip()
        chans = [c for c in re.split(r"[,\s]+", str(body.get("channels") or "")) if c]
        if key in RESERVED_SENDERS:
            _note(f"config\\guests.ini [guest.{label}] uses the reserved name "
                  f"'{key}' - ignored; rename it")
            continue
        # Desk mail (docs\DELEGATION.md D2) made session ids first-class `from`
        # values, so a guest label must never look like one: a dot is the desk-id
        # shape (project.component, tool.name), `orchestrator`/`daybook` are the
        # dotless desks, and `*-job` is the tool-handoff tag. A guest wearing any
        # of those would be classified as the fleet - mail from a person, treated
        # as nobody waiting. Fails closed like everything else here.
        if "." in key or key in ("orchestrator", "daybook") or key.endswith("-job"):
            _note(f"config\\guests.ini [guest.{label}] looks like a desk id or "
                  f"fleet tag - ignored; pick a plain name (no dot, not "
                  f"'orchestrator'/'daybook', no -job suffix)")
            continue
        if not _SNOWFLAKE.match(uid):
            _note(f"config\\guests.ini [guest.{label}] user_id "
                  f"'{uid or '(missing)'}' is not a Discord id (17-20 digits) "
                  f"- entry ignored")
            continue
        if not chans:
            _note(f"config\\guests.ini [guest.{label}] names no channels - entry "
                  f"ignored (a guest with no channel list is never given all of them)")
            continue
        out[key] = {"id": uid, "channels": chans,
                    "name": str(body.get("name") or label).strip(),
                    "scope": str(body.get("scope") or "").strip()}
    return out


def websites(env=None):
    """-> {site: {"url", "user", "password_env", "selectors": {...}}} per
    `[<site>]` in config\\websites.ini (docs\\WEB.md W1).

    A REGISTRY, not a vault: the block names the .env KEY that holds the
    password (`password_env`, or `password_key` - his spelling - as an alias),
    never the password. Desks are denied from reading .env, so the secret stays
    out of the model entirely; only tools\\playwright\\weblogin.py resolves it.

    Sections are bare site names (`[ionos]`), not the `group()` prefix shape:
    this file has exactly one kind of entry, and a stranger writing it by hand
    should not have to learn a prefix.

    FAILS CLOSED like guests(): a block with no `url` is ignored and reported,
    because a login target nobody can verify is not a target. A site with a
    user and no password_env is LEGAL and means the safest thing - sign in by
    hand in the browser once, let the session persist, script nothing.
    """
    out = {}
    for site, body in sorted(load("websites").items()):
        key = str(site).strip().lower()
        body = body or {}
        url = str(body.get("url") or "").strip()
        if not url:
            _note(f"config\\websites.ini [{site}] names no url - entry ignored")
            continue
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        out[key] = {
            "url": url,
            "user": str(body.get("user") or "").strip(),
            # his spelling first-class: password_key is the same thing
            "password_env": str(body.get("password_env")
                                or body.get("password_key") or "").strip(),
            "selectors": {k: str(body.get(k) or "").strip()
                          for k in ("user_selector", "pass_selector",
                                    "submit_selector", "otp_selector",
                                    "success_selector")
                          if str(body.get(k) or "").strip()},
        }
    return out


def website_status(env=None):
    """-> list of (site, url, user, env_key, 'set'|'NOT SET'|'browser session').

    Same reason account_status exists: these are DISCOVERED from config, so a
    fixed SECRET_KEYS tuple cannot describe them. Values are never returned -
    only whether the named key has one.
    """
    env = load_env() if env is None else env
    out = []
    for site, body in sorted(websites().items()):
        env_key = body.get("password_env") or ""
        if not env_key:
            state = "browser session"       # signs in by hand; nothing scripted
        else:
            state = "set" if env_value(env_key, env) else "NOT SET"
        out.append((site, body.get("url", ""), body.get("user", ""), env_key, state))
    return out


class _EverySkill(frozenset):
    """A set that CONTAINS any skill name, and lists the installed ones.

    The open default cannot be "every folder under .claude\\skills\\": plenty of
    skills are not folders at all - `/code-review`, `/simplify`, `/run` and the
    rest arrive with Claude Code or a plugin, and a folder scan silently made
    them unavailable. That would have been NARROWER than the explicit list this
    replaced, which is the opposite of the intent.

    So membership is open (owner mail only - the sender gate is what matters
    and it is unchanged), while iteration still yields what is installed, so
    `!config` can show something concrete instead of the word "everything".
    """

    def __contains__(self, item):
        return bool(re.fullmatch(r"[a-z0-9_-]+", str(item or "").strip().lower()))


def installed_skills():
    """-> every skill this instance actually has, by folder name.

    The disk is the list. A skill exists because `.claude\\skills\\<name>\\
    SKILL.md` exists, so a skill added by an update is usable the moment it
    lands, with nothing to re-declare.
    """
    out = set()
    for base in (ROOT / ".claude" / "skills", ROOT / "daybook" / ".claude" / "skills"):
        try:
            for d in base.iterdir():
                if (d / "SKILL.md").is_file() and re.fullmatch(r"[a-z0-9_-]+", d.name):
                    out.add(d.name)
        except OSError:
            continue
    return out


def slash_skills():
    """-> set of skill names owner mail may fire with `/<name>` (docs\\DELEGATION.md D6).

    OPEN BY DEFAULT (owner decision 2026-08-19). With no `config\\skills.ini`,
    every installed skill is available - because the person who installed this
    IS the owner, and `skills.ini` never shipped, so the closed default meant a
    fresh instance answered `/goal` with a refusal and no explanation of what to
    do about it. The gate that matters is WHO is asking, and that one is
    unchanged and fail-closed: guests and Telegram invitees never pass a slash,
    whatever this returns (the watchdog checks the sender first).

    Write `[skills] allowed = status, watch` to NARROW it deliberately; the file
    is now a restriction rather than an enrolment. `allowed = none` (or `off`)
    closes it entirely. Labels are validated `[a-z0-9_-]+`; anything else is
    reported through problems() and skipped.
    """
    raw = str((load("skills").get("skills") or {}).get("allowed") or "").strip()
    if not raw:
        return _EverySkill(installed_skills())
    if raw.lower() in ("none", "off", "-"):
        return set()
    out = set()
    for name in re.split(r"[,\s]+", raw):
        if not name:
            continue
        n = name.strip().lower().lstrip("/")
        if re.fullmatch(r"[a-z0-9_-]+", n):
            out.add(n)
        else:
            _note(f"config\\skills.ini: '{name}' is not a skill name "
                  f"([a-z0-9_-] only) - skipped")
    return out


TELEGRAM_VISIBILITY = ("all", "own")


def _desk_folder(desk):
    """desk id -> the folder that desk sits in. Mirrors watchdog.cwd_for.

    Duplicated rather than imported: watchdog.py is the always-on brain and
    importing it to read a config file would be the wrong dependency. Keep the
    two in step - they answer the same question.
    """
    desk = str(desk or "").strip().lower()
    if desk == "orchestrator":
        return ROOT
    if desk == "daybook":
        return ROOT / "daybook"
    if desk.startswith("tool."):
        return ROOT / "tools" / desk.split(".", 1)[1]
    project, _, component = desk.partition(".")
    return ROOT / "projects" / project / component


def telegram_chats():
    """-> {label: {...}} per `[chat.<label>]` in config\\telegram.ini.

    Invites someone with NO Discord account into exactly ONE channel, through a
    Telegram bot (tools\\telegram\\bridge.py, 2026-08-18). The label is what
    everyone sees: the Discord post, the envelope's `from`, the transcript and
    !trace. Desks need no new rule for these people - any `from` that is not a
    fleet tag is already treated as a guest (watchdog.is_human_sender), which
    is why this ships without touching guests.ini: THAT file gates Discord
    accounts, and an invited Telegram user has none.

    FAILS CLOSED, like guests() and for the same reason: it is the same kind
    of list and a generous reading of a typo is how one becomes a hole:

    - a `telegram_user_id` that is not digits, or a `telegram_username` that is
      not a handle -> ignored. A half-pasted id must never widen into "anyone".
    - neither of those, or no `discord_channel` -> ignored. A person with no
      name and nowhere to write is not a permission. (`desk` IS optional: the
      channel already knows which desk answers it.)
    - the same person named twice, or `visibility = own` in a channel somebody
      else also reaches -> ignored, because neither can be honoured as written.
    - a label that collides with a fleet sender, looks like a desk id, or ends
      in -job -> ignored, so an invited stranger can never arrive wearing one of
      the fleet's own `from` values.
    - an unknown `visibility` -> ignored rather than guessed: the choice decides
      whether the owner's other messages in that channel are shown to them.
    - anything unreadable -> {} (rule 3: config never raises).

    Every rejection is reported through problems(), never swallowed.
    """
    out = {}
    for label, body in sorted(group(load("telegram"), "chat").items()):
        key = str(label).strip().lower()
        body = body or {}
        where = f"config\\telegram.ini [chat.{label}]"
        if key in RESERVED_SENDERS or "." in key or key.endswith("-job") \
                or key in ("orchestrator", "daybook"):
            _note(f"{where} uses a name the fleet reserves for itself - ignored; "
                  f"pick a plain name (no dot, no -job, not orchestrator/daybook)")
            continue
        # EITHER a numeric id OR an @username. The username exists because the
        # id does not: nobody knows their own Telegram user id, and asking the
        # owner to fish one out of a log file before he can invite anyone was
        # the least friendly step in the whole system. The bridge pins the id
        # the first time that username writes, so a username released and
        # re-registered by someone else cannot inherit the invite.
        uid = str(body.get("telegram_user_id") or "").strip()
        uname = str(body.get("telegram_username") or "").strip().lstrip("@").lower()
        if uid and not uid.isdigit():
            _note(f"{where} telegram_user_id '{uid}' is not a number - entry "
                  f"ignored (nobody is let in by a typo)")
            continue
        if uname and not re.fullmatch(r"[a-z0-9_]{4,32}", uname):
            _note(f"{where} telegram_username '{uname}' is not a Telegram handle "
                  f"(letters, digits and _ only) - entry ignored")
            continue
        if not uid and not uname:
            _note(f"{where} needs telegram_username (their @handle) or "
                  f"telegram_user_id - entry ignored")
            continue
        channel = str(body.get("discord_channel") or "").strip().lstrip("#")
        if not channel:
            _note(f"{where} needs a discord_channel - entry ignored")
            continue
        # `desk` is OPTIONAL, and that is the point: the channel already knows
        # its desk (#web in my-project IS my-project.web - the map every message
        # the owner types goes through). Asking for it twice only created a way
        # to disagree with the fleet. Set it only to override that pairing, or
        # for a channel the fleet maps to nothing.
        desk = str(body.get("desk") or "").strip().lower()
        vis = str(body.get("visibility") or "own").strip().lower()
        if vis not in TELEGRAM_VISIBILITY:
            _note(f"{where} visibility '{vis}' is not one of "
                  f"{'/'.join(TELEGRAM_VISIBILITY)} - entry ignored rather than guessed "
                  f"(it decides whether your other messages are shown to them)")
            continue
        twin = next((l for l, b in out.items()
                     if (uid and b["telegram_user_id"] == uid)
                     or (uname and b["telegram_username"] == uname)), None)
        if twin:
            # ONE person reaches ONE channel. Listed twice, the bridge would have
            # matched whichever block sorted first and dropped the other in
            # silence - the person would write into a channel nobody told them
            # about. Refusing both halves of the ambiguity is the honest answer;
            # inviting the same person to a second channel needs a second
            # Telegram account, or a decision about which channel they belong to.
            _note(f"{where} names the same person as [chat.{twin}] - one person "
                  f"reaches ONE channel, so this entry is ignored (give them one "
                  f"block, not two)")
            continue
        if desk and not _desk_folder(desk).is_dir():
            # REPORTED, not rejected, and the difference is deliberate. The other
            # rejections above are ambiguity or impersonation - things that must
            # never be guessed. A desk folder that is not there is a typo (or a
            # project not created YET, which is a legitimate order of operations),
            # and it announces itself the moment they write: the run fails and
            # the watchdog alerts. Silently admitting nobody would be the quieter
            # and worse answer.
            _note(f"{where} desk '{desk}' has no folder at {_desk_folder(desk)} - "
                  f"they will be let in, but nothing will answer until that desk "
                  f"exists (orchestrator / daybook / tool.<name> / <project>.<component>)")
        out[key] = {"telegram_user_id": uid, "telegram_username": uname,
                    "discord_channel": channel, "desk": desk,
                    "visibility": vis,
                    "media_out": str(body.get("media_out") or "0").strip().lower()
                    in TRUE,
                    "name": str(body.get("name") or label).strip()}

    # A PROMISE THAT CANNOT BE KEPT IS REFUSED, not quietly broken.
    #
    # `own` means "your messages and Omnius's replies". In a room with one guest
    # that is exact. With two, the desk's answer to the OTHER person is just an
    # ordinary bot message in the same channel - nothing distinguishes it - so
    # `own` would hand each of them the other's answers while promising privacy.
    # Found by review before anyone was invited into such a room.
    #
    # Refusing the entry rather than silently widening it to `all`: the owner
    # chose `own` for a reason, and the fix is his to make (separate channels, or
    # `all` for everyone in that room).
    # Grouped case-insensitively. It still cannot see that an id and a #name are
    # the same room - config has no Discord connection - so the bridge repeats
    # this check once the channels are resolved, where it can.
    rooms = {}
    for label, b in out.items():
        rooms.setdefault(b["discord_channel"].lower(), []).append(label)
    for channel, labels in rooms.items():
        if len(labels) < 2:
            continue
        for label in list(labels):
            if out[label]["visibility"] == "own":
                _note(f"config\\telegram.ini [chat.{label}] asks for visibility=own "
                      f"in #{channel}, which {len(labels)} people share - a reply "
                      f"to one of them is an ordinary message to the others, so "
                      f"'own' cannot be honoured there. Entry ignored: give them "
                      f"separate channels, or set visibility=all for everyone in "
                      f"that one.")
                del out[label]
    return out


def telegram_status(env=None):
    """-> [(label, telegram_user_id, channel, desk, visibility, token state)].

    Discovered from config like guests and mail accounts, so it cannot live in
    SPEC and needs its own reporter for !config. The token is only ever
    described as set / NOT SET.
    """
    env = load_env() if env is None else env
    cfg = load("telegram")
    env_key = str((cfg.get("telegram") or {}).get("token_env")
                  or "TELEGRAM_BOT_TOKEN").strip()
    state = "set" if env_value(env_key, env) else "NOT SET"
    # An empty desk is the normal case, not a gap: the channel's own desk
    # answers. Saying so beats printing a blank column.
    return [(label, b["telegram_user_id"], b["discord_channel"],
             b["desk"] or "(the channel's own desk)",
             b["visibility"], f"{env_key}: {state}")
            for label, b in sorted(telegram_chats().items())]


def guest_status():
    """-> list of (label, name, user_id, channels) for every USABLE guest.

    Discovered from config like mail accounts, so it cannot live in SPEC. The id
    is shown in full on purpose: a wrong or half-pasted id is the first thing to
    suspect when a guest's messages go nowhere, and a Discord user id is public
    identity, not a credential.
    """
    return [(label, g["name"], g["id"], ", ".join(g["channels"]))
            for label, g in sorted(guests().items())]


def effective(env=None):
    """-> list of (name, key, value, source) for every known setting.

    `source` is one of env / config\\<file> / default, so the answer to "why is
    it this value?" comes with the reason. Secrets are NOT included here; see
    secret_status().
    """
    env = load_env() if env is None else env
    rows, cache = [], {}
    for name, section, key, env_var, default, kind in SPEC:
        cfg = cache.setdefault(name, load(name))
        live = env_value(env_var, env) if env_var else ""
        in_file = str((cfg.get(section) or {}).get(key) or "").strip()
        if live:
            value, source = live, f"env {env_var}"
        elif in_file:
            value, source = in_file, f"config\\{name}.ini"
        else:
            value, source = default, "default"
        rows.append((name, key, value, source))
    return rows


def secret_status(env=None):
    """-> list of (env_key, 'set'|'NOT SET'). Never the value itself."""
    env = load_env() if env is None else env
    return [(k, "set" if env_value(k, env) else "NOT SET") for k in SECRET_KEYS]


def browser_device_id(env=None):
    """-> the Chrome extension deviceId a desk must drive, or "".

    The Chrome extension REFUSES to act while more than one browser is
    connected: it requires the session to ask which one, and the answer is a
    click in Chrome on this machine. From a phone that click does not exist,
    so the whole browser capability was unreachable remotely (2026-08-13, his
    words: "how am i gonna accept if i am on mobile on discord? we have a
    problem here" - and he was right).

    Settling it here turns a question that needs physical access into a
    setting decided ONCE, at the desk. A desk reads this and calls
    select_browser with it instead of asking. Empty means "ask" - which is
    correct on an install that has only one browser, where nothing is asked
    anyway.
    """
    return get(load("omnius"), "browser", "device_id", "OMNIUS_BROWSER_DEVICE_ID", "", env).strip()


def agent_name(env=None):
    r"""What this instance is CALLED: "Omnius", "Jarvis", "Maikel"...

    His ask, 2026-08-24: "i want to include the agent name to a config file,
    how he will be addressed to". Only the ADDRESS changes. The install
    folder stays `omnius` and so does the `/omnius` skill every run is
    started with - those are machinery, and renaming them would break every
    path and every doc for a cosmetic gain.

    Consumed by: the orchestrator's channel name, the terminal tab title,
    and the desks themselves (root CLAUDE.md par. 3). Changing it later is
    safe because Discord routing is by channel ID, not by name.
    """
    raw = get(load("omnius"), "omnius", "name", "OMNIUS_NAME", "", env)
    # One line, printable, short enough for a Discord channel name.
    raw = "".join(c for c in raw if c.isprintable()).strip()[:32].strip()
    return raw or "Omnius"


def agent_slug(env=None):
    """agent_name() as a Discord channel name: lowercase, kebab, no emoji."""
    slug = re.sub(r"[^a-z0-9-]+", "-", agent_name(env).lower()).strip("-")
    return slug or "omnius"


def backup_folder(env=None):
    """-> (Path or None, status string) for where this instance keeps its backups.

    EVERY INSTANCE BACKS ITSELF UP. The repo is only the system; a machine's
    notes, projects and media exist nowhere else, so an unset backup folder
    means one disk failure from losing all of it. That is why this is the one
    unset setting that complains (see problems()) instead of quietly using a
    default - there is no sane default for "somewhere that is not this disk".

    It replaces guessing. The old procedure found the destination by looking
    for a `OneDrive - *` folder by name, which worked on exactly one machine
    and gives a fresh install nothing.
    """
    raw = get(load("omnius"), "backup", "folder", "OMNIUS_BACKUP_DIR", "", env)
    if not raw:
        return None, "NOT SET — backups have nowhere to go"
    p = Path(raw).expanduser()
    if not p.exists():
        return p, f"set, but does not exist: {p}"
    if not p.is_dir():
        return p, f"set, but is not a folder: {p}"
    return p, f"ready: {p}"


def problems():
    """-> list[str] of human-readable faults found while reading. [] = usable.

    Shape complaints only. Emptiness is deliberately NOT an error here: every
    setting has a working default, and the Discord credentials are validated by
    api.config_problems(), which knows what a snowflake looks like.
    """
    out = list(_problems)
    if CONFIG_DIR.exists() and not CONFIG_DIR.is_dir():
        out.append(f"{CONFIG_DIR} exists but is not a folder")
    # The one emptiness that IS an error. Everything else here has a working
    # default; "nowhere to back up to" does not, and staying quiet about it
    # until the disk dies is not a default, it is a bet.
    _bp, _bs = backup_folder()
    if _bp is None:
        out.append("NO BACKUP FOLDER SET — this instance's notes, projects and media "
                   "exist on this disk only. Set `[backup] folder` in config\\omnius.ini "
                   "(a synced folder: OneDrive, Dropbox, a NAS, another drive).")
    elif not _bp.is_dir():
        out.append(f"backup folder {_bs} — nothing can be copied there")
    for name, section, key, env_var, default, kind in SPEC:
        cfg = load(name)
        raw = str((cfg.get(section) or {}).get(key) or "").strip()
        if not raw:
            continue
        if kind == "int":
            try:
                int(raw)
            except ValueError:
                out.append(f"config\\{name}.ini [{section}] {key}={raw!r} is not a number")
        elif kind == "bool" and raw.lower() not in TRUE + FALSE:
            out.append(f"config\\{name}.ini [{section}] {key}={raw!r} is not true/false")
    # Mail has three distinct not-ready states and they need different fixes.
    # Silence about which one you are in is what makes credential problems feel
    # like network problems. NONE of them is an error on its own: no email.ini
    # at all is the normal state of an instance that does not use mail.
    if ini_path("email").is_file():
        accounts = account_status()
        if not accounts:
            out.append("config\\email.ini has no [account.<label>] section yet "
                       "(every key in the template ships commented out)")
        for label, _user, env_key, state in accounts:
            if state == "no password_env":
                out.append(f"config\\email.ini [account.{label}] names no credential: "
                           f"it needs password_env (IMAP) or token_env (Graph) "
                           f"pointing at the .env key that holds it")
            elif state == "NOT SET":
                out.append(f"config\\email.ini [account.{label}] names {env_key}, "
                           f"which is not set in .env")
    # Guests are an authorisation list, so every REJECTED entry has to be
    # visible: "she writes and nothing happens" is precisely the silent failure
    # this feature was added to remove, and guests() has just recorded its own
    # rejections into _problems by being called here.
    if ini_path("guests").is_file():
        guests()
        if not group(load("guests"), "guest"):
            out.append("config\\guests.ini has no [guest.<label>] section yet "
                       "(every key in the template ships commented out)")
    # A capability half-configured is worth saying out loud: a provider chosen
    # with no usable key looks enabled in the file and silently is not.
    for name, provider, env_key, state in capability_status():
        if state == "NO KEY":
            out.append(f"config\\ai.ini [{name}] selects {provider!r} but "
                       f"{env_key or 'api_key_env'} is not set in .env — "
                       f"{name} stays off")
    return out


def describe():
    """The human dump behind `!config`. Secrets appear as set/NOT SET only."""
    lines = [f"**config @ {CONFIG_DIR}**" if CONFIG_DIR.is_dir()
             else f"**no config\\ folder yet — every value is a default** ({CONFIG_DIR})"]
    for name, key, value, source in effective():
        lines.append(f"`{name}.{key}` = `{value}`  ·  {source}")
    lines.append("**secrets** (root `.env`, values never shown)")
    for env_key, state in secret_status():
        lines.append(f"`{env_key}` — {state}")
    # Only shown once mail is actually configured: a fresh instance that has
    # never touched email should see nothing about it (optional means optional).
    accounts = account_status()
    if accounts:
        lines.append("**mail accounts** (passwords live in `.env`)")
        for label, user, env_key, state in accounts:
            who = f" `{user}`" if user else ""
            lines.append(f"`{label}`{who} — {env_key or 'password_env missing'}: {state}")
    # Only shown once someone is actually configured - an instance with no
    # guests should see nothing about them.
    people = guest_status()
    if people:
        lines.append("**guests** (may write only in the channels listed)")
        for label, name, uid, chans in people:
            who = f" `{name}`" if name and name.lower() != label else ""
            lines.append(f"`{label}`{who} — id `{uid}` · {chans}")
    # Only shown once sites are configured - an instance that drives no
    # websites should see nothing about them.
    sites = website_status()
    if sites:
        lines.append("**websites** (passwords live in `.env`, never here)")
        for site, url, user, env_key, state in sites:
            who = f" `{user}`" if user else ""
            lines.append(f"`{site}`{who} — {url} · {env_key or 'no key'}: {state}")
    # Only shown once someone is actually invited - an instance with no bridge
    # should see nothing about Telegram.
    tg = telegram_status()
    if tg:
        lines.append("**telegram** (invited people; the bot token lives in `.env`)")
        for label, uid, channel, desk, vis, tok in tg:
            lines.append(f"`{label}` — tg `{uid}` → `#{channel}` · desk `{desk}` · "
                         f"sees `{vis}` · {tok}")
    _sk = slash_skills()
    passes = sorted(_sk)
    if isinstance(_sk, _EverySkill):
        lines.append("**slash pass-through** (owner mail only): **every skill** — open by "
                     "default, no `config\\skills.ini` needed. Installed here: "
                     + (", ".join(f"`/{s}`" for s in passes) or "(none)"))
    else:
        lines.append("**slash pass-through** (`config\\skills.ini` NARROWS it, owner mail "
                     "only): "
                     + (", ".join(f"`/{s}`" for s in passes) if passes
                        else "(none — closed by `allowed = none`)"))
    caps = capability_status()
    if any(state != "off" for _n, _p, _k, state in caps):
        lines.append("**AI capabilities** (keys live in `.env`)")
        for name, provider, env_key, state in caps:
            if state == "off":
                lines.append(f"`{name}` — off")
            else:
                lines.append(f"`{name}` — `{provider}`"
                             f"{f' · {env_key}' if env_key else ' · no key needed'}"
                             f": {state}")
    bad = problems()
    if bad:
        lines.append("**problems**")
        lines.extend(f"⚠️ {b}" for b in bad)
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(describe())
