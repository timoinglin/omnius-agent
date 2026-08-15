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
        return {s: dict(parser[s]) for s in parser.sections()}
    return {}


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
    ("omnius", "omnius", "machine_name", "MACHINE_NAME", "", "str"),
    ("omnius", "omnius", "language", "OMNIUS_LANGUAGE", "", "str"),
    ("omnius", "omnius", "heartbeat_minutes", "HEARTBEAT_MINUTES", "30", "int"),
    ("omnius", "omnius", "gateway", "DISCORD_GATEWAY", "1", "bool"),
    ("omnius", "backup", "folder", "OMNIUS_BACKUP_DIR", "", "str"),
    ("omnius", "browser", "device_id", "OMNIUS_BROWSER_DEVICE_ID", "", "str"),
    # Desk mail (docs\DELEGATION.md). hop_ttl = forward hops a delegation chain
    # may spend (replies are free); loop_budget = scheduled continuation runs
    # before a loop must checkpoint with the owner (consumed by schedule.py in
    # Phase D); cross_project_requires_ok = hold cross-project desk mail for an
    # ok in Discord - ON by default, an authorisation gate fails closed.
    ("omnius", "delegation", "hop_ttl", "OMNIUS_HOP_TTL", "3", "int"),
    ("omnius", "delegation", "loop_budget", "OMNIUS_LOOP_BUDGET", "5", "int"),
    ("omnius", "delegation", "cross_project_requires_ok", "OMNIUS_CROSS_PROJECT_OK", "1", "bool"),
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


def slash_skills():
    """-> set of skill names owner mail may fire with `/<name>` (docs\\DELEGATION.md D6).

    `config\\skills.ini`, `[skills] allowed = status, watch`. An AUTHORISATION
    list, so it fails closed like guests(): missing file, missing key or an
    empty value = NOTHING passes. Labels are validated `[a-z0-9_-]+`; anything
    else is reported through problems() and skipped. Guests never pass slashes
    regardless of this list - the watchdog checks the sender first."""
    out = set()
    raw = str((load("skills").get("skills") or {}).get("allowed") or "")
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
    passes = sorted(slash_skills())
    lines.append("**slash pass-through** (`config\\skills.ini`, owner mail only): "
                 + (", ".join(f"`/{s}`" for s in passes) if passes else "(none - closed)"))
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
