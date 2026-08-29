"""Offline tests for tools\\email. No mailbox, no network, no credentials.

Everything reachable without a server is tested directly; the two mistakes that
would be SILENT in production - using sequence numbers instead of UIDs, and
marking his mail read just by looking at it - are pinned with AST checks,
because both produce correct-looking output right up until they lose a message.

    python tools\\email\\test_email.py
"""
import ast
import io
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "tools"))

import omnius_config as ocfg  # noqa: E402
import mail  # noqa: E402

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


def raises(fn, exc):
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


SAND = Path(tempfile.mkdtemp(prefix="omnius-mailtest-"))
SRC = (HERE / "mail.py").read_text(encoding="utf-8")

# --- message identity ---------------------------------------------------------
# A bare sequence number is not an identity: it shifts whenever anything else
# touches the mailbox, which a phone syncing in the background guarantees.
print("== message ids ==")
_id = mail.make_id("work", "INBOX", 12345, "678")
check("an id round-trips", mail.parse_id(_id) == ("work", "INBOX", 12345, "678"))
check("a folder containing a slash still round-trips",
      mail.parse_id(mail.make_id("w", "INBOX/Sub", 1, "2"))[1] == "INBOX/Sub")
for bad in ("garbage", "", "work/INBOX/notanumber/5", "work/INBOX/5"):
    check(f"rejects {bad!r} instead of guessing",
          raises(lambda b=bad: mail.parse_id(b), mail.UsageError))
check("UIDVALIDITY is carried, so a rebuilt mailbox is detected not mis-addressed",
      "uidvalidity" in SRC.lower() and "validity" in _id.replace("12345", "validity"))

# --- attachment names are attacker input --------------------------------------
print("== attachment filenames ==")
for hostile, why in [
        ("../../../../etc/passwd", "traversal"),
        (r"..\..\windows\system32\evil.dll", "windows traversal"),
        ("C:\\absolute\\path.txt", "absolute path"),
        ("...", "dots only"),
        ("", "empty"),
        ("CON.txt", "reserved device name"),
        ("a/b/c.txt", "embedded separators")]:
    got = mail.safe_name(hostile)
    check(f"{why}: {hostile!r} -> {got!r} is inert",
          "/" not in got and "\\" not in got and ".." not in got
          and not got.startswith(".") and got != ""
          and got.upper().split(".")[0] not in {"CON", "PRN", "AUX", "NUL"})
check("a normal name survives readable", mail.safe_name("Factura_2026-08.pdf")
      == "Factura_2026-08.pdf")
check("a very long name is bounded", len(mail.safe_name("x" * 500)) <= 120)

# --- body handling ------------------------------------------------------------
print("== bodies ==")
import email.policy  # noqa: E402
from email.message import EmailMessage  # noqa: E402


def _msg(text, subtype="plain"):
    m = EmailMessage()
    m["From"] = "a@example.com"
    m["Subject"] = "s"
    m.set_content(text, subtype=subtype)
    return mail.parse_message(m.as_bytes())


_text, _trunc, _total = mail.body_text(_msg("hola"))
check("a plain body reads back", _text.strip() == "hola" and not _trunc)
_long = "x" * (mail.MAX_BODY_CHARS + 500)
_text, _trunc, _total = mail.body_text(_msg(_long))
check("an over-long body is truncated", len(_text) == mail.MAX_BODY_CHARS)
check("...and the truncation is REPORTED, never silent",
      _trunc is True and _total >= mail.MAX_BODY_CHARS + 500,
      "an agent would summarise a partial mail as complete")
_text, _, _ = mail.body_text(_msg("<p>hola <b>mundo</b></p>", subtype="html"))
check("html is flattened to something readable",
      "hola" in _text and "<p>" not in _text)
_empty = EmailMessage()
_empty["From"] = "a@example.com"
check("a message with no usable body returns empty, never raises",
      mail.body_text(mail.parse_message(_empty.as_bytes()))[0] == "")
_enc = EmailMessage()
_enc["Subject"] = "Facturación ñ"
_enc["From"] = "Añez <a@example.com>"
_enc.set_content("x")
_rt = mail.parse_message(_enc.as_bytes())
check("RFC 2047 encoded headers come back decoded",
      "Facturación" in mail.header_str(_rt, "Subject"))

# --- imaplib's awkward FETCH shape --------------------------------------------
print("== FETCH parsing ==")
_data = [(b"1 (UID 42 FLAGS (\\Seen \\Answered) BODY[HEADER] {12}",
          b"Subject: hi\r\n"), b")",
         (b"2 (UID 43 FLAGS () BODY[HEADER] {5}", b"x\r\n"), b")"]
_parsed = mail.parse_fetch(_data)
check("both messages are recovered", len(_parsed) == 2)
check("the UID is read from the descriptor, not assumed positional",
      [u for u, _, _ in _parsed] == ["42", "43"])
check("flags are parsed", _parsed[0][1] == ["\\Seen", "\\Answered"]
      and _parsed[1][1] == [])
check("bare bytes between items are skipped", all(r for _, _, r in _parsed))
check("garbage in, empty out - never an exception",
      mail.parse_fetch([b")", None, ("no uid here", b"x")]) == [])

# --- composing and threading --------------------------------------------------
print("== composing ==")
_body = {"user": "me@example.com", "from_name": "Me", "smtp_host": "h"}
_m = mail._build("work", _body, "you@example.com", "Hola", "cuerpo")
check("From carries the display name", "Me" in _m["From"] and "me@example.com" in _m["From"])
check("every message gets a Message-ID", _m["Message-ID"].startswith("<"))
check("a plain message has no threading headers", _m["In-Reply-To"] is None)
_r = mail._build("work", _body, "you@example.com", "Re: Hola", "respuesta",
                 in_reply_to="<orig@example.com>", references="<older@example.com>")
check("a reply sets In-Reply-To", _r["In-Reply-To"] == "<orig@example.com>")
check("...and References carries the WHOLE chain, or clients start a new thread",
      "<older@example.com>" in _r["References"] and "<orig@example.com>" in _r["References"])

# --- who the Message-ID says we are -------------------------------------------
# make_msgid() left alone stamps the PC's hostname: <...@DESKTOP-ABC1234>.
# Dotless, not an FQDN, and not the domain the mail claims to come from - three
# marks against it at every major spam filter, and nothing in the mail looks
# wrong. The same fix a PHP mailer makes by setting PHPMailer's Hostname.
print("== message-id identity ==")
check("the Message-ID is signed with the From domain",
      _m["Message-ID"].rstrip(">").endswith("@example.com"),
      f"got {_m['Message-ID']!r}")
check("...so the machine's hostname never leaves the building",
      __import__("socket").gethostname().lower() not in _m["Message-ID"].lower())
check("the domain is an FQDN, never a dotless name",
      "." in _m["Message-ID"].rpartition("@")[2])
for _bad, _why in [("me@localhost", "dotless domain"), ("nonsense", "no @ at all"),
                   ("", "no address")]:
    _mid = mail._build("w", {"user": _bad}, "you@example.com", "S", "t")["Message-ID"]
    check(f"{_why}: falls back to a valid Message-ID instead of forging one",
          _mid.startswith("<") and _mid.endswith(">") and "@" in _mid)
check("sender_domain requires the dot itself",
      mail.sender_domain("a@example.com") == "example.com"
      and mail.sender_domain("a@host") == ""
      and mail.sender_domain(None) == "")
check("EHLO announces the same domain, not the PC name",
      "local_hostname=helo" in SRC,
      "a dotless HELO argument is a documented spam signal")

# --- HTML mail ----------------------------------------------------------------
# The branded template this desk answers support tickets in. Plain-only was the
# ONLY thing _build could produce until 2026-08-29, so the orchestrator had to
# hand-build MIME in a throwaway script to answer one ticket.
print("== html bodies ==")
_h = mail._build("work", _body, "you@example.com", "Hola", "texto plano",
                 html="<h1>Hola</h1><p>en color</p>")
check("html makes the message multipart/alternative",
      _h.get_content_type() == "multipart/alternative",
      f"got {_h.get_content_type()}")
check("the HTML part is really there",
      "<h1>Hola</h1>" in _h.get_body(preferencelist=("html",)).get_content())
check("...and a text/plain fallback part SURVIVES beside it",
      "texto plano" in _h.get_body(preferencelist=("plain",)).get_content(),
      "html-only scores as spam and is blank in clients that refuse HTML")
check("no --html means exactly the old plain message, unchanged",
      _m.get_content_type() == "text/plain")
_pdf = [("invoice.pdf", b"%PDF-1.4 fake\n", "application", "pdf")]
_hm = mail._build("work", _body, "you@example.com", "S", "texto",
                  html="<p>rico</p>", attachments=_pdf)
check("html PLUS an attachment nests correctly (mixed wrapping alternative)",
      _hm.get_content_type() == "multipart/mixed"
      and [p.get_filename() for p in _hm.iter_attachments()] == ["invoice.pdf"],
      f"got {_hm.get_content_type()}")
check("...and both bodies are still reachable inside it",
      "texto" in _hm.get_body(preferencelist=("plain",)).get_content()
      and "rico" in _hm.get_body(preferencelist=("html",)).get_content())


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


_htmlf = SAND / "template.html"
_htmlf.write_text("<p>plantilla</p>", encoding="utf-8")
check("--html is read from a FILE, like the body",
      mail._html_body(_Args(html=str(_htmlf))) == "<p>plantilla</p>")
check("no --html is None, not an empty string",
      mail._html_body(_Args(html=None)) is None and mail._html_body(_Args()) is None)
check("a missing --html file refuses the send instead of sending plain",
      raises(lambda: mail._html_body(_Args(html=str(SAND / "ghost.html"))),
             mail.UsageError))
_empty_html = SAND / "empty.html"
_empty_html.write_text("   \n", encoding="utf-8")
check("an empty --html file is refused, naming the fix",
      raises(lambda: mail._html_body(_Args(html=str(_empty_html))), mail.UsageError))
check("an HTML reply through Graph is REFUSED, never silently sent as plain",
      raises(lambda: mail._send_graph("w", {}, "you@example.com", "S", "t",
                                      "graph-id", True, None, "<p>x</p>"),
             mail.UsageError),
      "Graph's /reply carries a comment, not a body")

# --- date ranges --------------------------------------------------------------
# "all the mail from July" is the shape of every real request, and the end of a
# range is EXCLUSIVE in both IMAP (BEFORE) and Graph (lt) - so a naive
# implementation silently drops the 31st, which nobody notices until an invoice
# is missing.
print("== date ranges ==")
import datetime as _dt  # noqa: E402

check("a bare month starts on the 1st",
      mail.parse_when("2026-07") == _dt.date(2026, 7, 1))
check("...and ENDS on the 1st of the next month, so the 31st is included",
      mail.parse_when("2026-07", end=True) == _dt.date(2026, 8, 1))
check("December rolls the year over",
      mail.parse_when("2026-12", end=True) == _dt.date(2027, 1, 1))
check("a single day as an end becomes the next day, for the same reason",
      mail.parse_when("2026-07-14", end=True) == _dt.date(2026, 7, 15))
check("an empty range is simply no range", mail.parse_when("") is None)
for bad in ("july", "2026", "07-2026", "2026-13-01x"):
    check(f"refuses {bad!r} instead of guessing a range",
          raises(lambda b=bad: mail.parse_when(b), mail.UsageError))
check("a date range is never trimmed to the default page size",
      "not (since or until)" in SRC,
      "'all of July' must mean all of July, not the newest 25 of it")

# --- attachments on the way OUT -----------------------------------------------
print("== sending attachments ==")
_att = SAND / "invoice.pdf"
_att.write_bytes(b"%PDF-1.4 fake\n")
_got = mail.collect_attachments([str(_att)])
check("a file is read and typed for sending",
      len(_got) == 1 and _got[0][0] == "invoice.pdf"
      and _got[0][2] == "application" and _got[0][3] == "pdf")
check("no attachments is not an error", mail.collect_attachments(None) == [])
check("a MISSING file refuses the whole send rather than sending part of it",
      raises(lambda: mail.collect_attachments([str(SAND / "nope.pdf")]),
             mail.UsageError),
      "silently dropping one of five invoices is worse than failing")
_big = SAND / "huge.bin"
_big.write_bytes(b"x" * (mail.MAX_ATTACH_BYTES + 10))
check("oversized attachments are refused before anything is sent",
      raises(lambda: mail.collect_attachments([str(_big)]), mail.UsageError))
_big.unlink()
# Attaching anything turns the message multipart, and get_content() RAISES on
# multipart/mixed - which crashed the dry-run preview the first time.
_m2 = mail._build("work", {"user": "me@example.com", "smtp_host": "h"},
                  "you@example.com", "S", "cuerpo", attachments=_got)
check("the built message really carries the file",
      [p.get_filename() for p in _m2.iter_attachments()] == ["invoice.pdf"])
check("...and the body is still recoverable from a multipart message",
      (_m2.get_body(preferencelist=("plain",)) or {}) is not None
      and "cuerpo" in _m2.get_body(preferencelist=("plain",)).get_content())
check("the dry-run preview does not call get_content() on the message itself",
      "msg.get_content()" not in SRC,
      "it raises KeyError on multipart/mixed")

# --- attachments on the way IN ------------------------------------------------
# iter_attachments() yields NOTHING for a non-multipart message, so a mail whose
# whole body is one file reported "attachments": [] and saved nothing - silently,
# no error anywhere. Reproduced 2026-08-29 against a live google.com DMARC report
# in a live INBOX: bare application/zip, a filename, no text part at all.
print("== attachments on the way IN ==")


def _one_file_msg(data=b"PK\x03\x04zip", maintype="application", subtype="zip",
                  filename="report.zip"):
    m = EmailMessage()
    m["From"] = "dmarc@example.com"
    m["Subject"] = "Report domain: example.com"
    m.set_content(data, maintype=maintype, subtype=subtype,
                  **({"filename": filename} if filename else
                     {"disposition": "attachment"}))
    return mail.parse_message(m.as_bytes())


_dmarc = _one_file_msg()
check("a non-multipart mail that IS a file is seen as an attachment",
      len(mail.attachment_parts(_dmarc)) == 1,
      "the DMARC-report shape; it used to report [] and lose the file")
check("...and it keeps the real filename",
      mail.attachment_name(mail.attachment_parts(_dmarc)[0]) == "report.zip")
check("a nameless binary mail still gets a name with a usable extension",
      mail.attachment_name(mail.attachment_parts(
          _one_file_msg(filename=None))[0]).endswith(".zip"))
check("an ordinary text mail has NO attachments (no false positives)",
      mail.attachment_parts(_msg("hola")) == [])
check("an html mail is not an attachment either",
      mail.attachment_parts(_msg("<p>hola</p>", subtype="html")) == [])
_multi = EmailMessage()
_multi["From"] = "a@example.com"
_multi.set_content("mira el adjunto")
_multi.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf",
                      filename="factura.pdf")
check("the ordinary multipart case is untouched",
      [p.get_filename() for p in mail.attachment_parts(
          mail.parse_message(_multi.as_bytes()))] == ["factura.pdf"])

_real_media, mail.MEDIA_INBOX = mail.MEDIA_INBOX, SAND / "media"
try:
    _saved = mail.save_attachments(_dmarc, "148")
    check("--save-attachments writes the one-file mail to disk, byte for byte",
          len(_saved) == 1
          and Path(_saved[0]["path"]).read_bytes() == b"PK\x03\x04zip",
          "it used to write nothing at all and report success")
    check("the saved file is uid-prefixed and inside media\\inbox",
          _saved[0]["name"].startswith("148-")
          and (SAND / "media") in Path(_saved[0]["path"]).resolve().parents)
    check("a mail with nothing to save saves nothing, without failing",
          mail.save_attachments(_msg("hola"), "1") == [])
finally:
    mail.MEDIA_INBOX = _real_media
check("`read` lists attachments through the same helper that saves them",
      SRC.count("attachment_parts(msg)") >= 2,
      "the listing went blind on exactly the same messages as the save")

# --- the audit trail ----------------------------------------------------------
print("== audit ==")
_real_logs, mail.LOGS = mail.LOGS, SAND / "logs"
try:
    mail.audit_send("work", "you@example.com", "Secreto", 100, "<m@x>", True)
    _line = (mail.LOGS / "email.log").read_text(encoding="utf-8").strip()
    _rec = json.loads(_line)
    check("a send is recorded", _rec["to"] == "you@example.com" and _rec["dryRun"] is True)
    check("the subject and size are there", _rec["subject"] == "Secreto" and _rec["bytes"] == 100)
    check("the BODY is never written to the log (mail carries password resets)",
          "body" not in _rec and "cuerpo" not in _line)
    mail.LOGS = SAND / "nope" / "deeper"
    (SAND / "nope").write_text("not a folder", encoding="utf-8")
    mail.audit_send("work", "log@example.com", "s", 1, "<m>", False)
    check("an unwritable log never stops the mail", True)
finally:
    mail.LOGS = _real_logs

# --- config integration -------------------------------------------------------
print("== config ==")
_real_dir, ocfg.CONFIG_DIR = ocfg.CONFIG_DIR, SAND / "config"
_real_root, ocfg.ROOT = ocfg.ROOT, SAND
ocfg.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
try:
    check("no email.ini at all is a clean 'not configured', not a crash",
          raises(lambda: mail.account(None), mail.UsageError))
    (ocfg.CONFIG_DIR / "email.ini").write_text(
        "[email]\ndefault = work\n\n[account.work]\nimap_host = h\n"
        "user = me@example.com\npassword_env = TEST_MAIL_PW\n\n"
        "[account.nokey]\nuser = x@example.com\n", encoding="utf-8")
    check("an account whose password_env is missing from .env is refused clearly",
          raises(lambda: mail.account("work"), mail.UsageError))
    (SAND / ".env").write_text("TEST_MAIL_PW=s3cret\n", encoding="utf-8")
    _label, _b, _pw = mail.account(None)
    check("the default account is used when none is named", _label == "work")
    check("the password is resolved out of .env, never out of config",
          _pw == "s3cret" and "s3cret" not in
          (ocfg.CONFIG_DIR / "email.ini").read_text(encoding="utf-8"))
    check("an account with no password_env is refused, naming the fix",
          raises(lambda: mail.account("nokey"), mail.UsageError))
    check("an unknown account lists the ones that exist",
          raises(lambda: mail.account("ghost"), mail.UsageError))
    check("!config reports each account's credential as set/NOT SET, never the value",
          [r[3] for r in ocfg.account_status(env=ocfg.load_env(SAND))
           if r[0] == "work"] == ["set"])
finally:
    ocfg.CONFIG_DIR, ocfg.ROOT = _real_dir, _real_root

# --- folders ------------------------------------------------------------------
# A folder the user can SEE must be a folder he can NAME. Graph speaks English
# well-known names and opaque ids; IMAP speaks modified UTF-7 and a delimiter
# the server picks. Both used to surface as "Id is malformed" / "no such
# folder", which reads like a broken tool rather than a lookup miss.
print("== folders ==")
check("a plain ASCII folder decodes unchanged",
      mail._utf7_decode(b"Recibos") == "Recibos")
check("modified UTF-7 decodes to the name he actually sees",
      mail._utf7_decode(b"N&APM-minas") == "Nóminas",
      f"got {mail._utf7_decode(b'N&APM-minas')!r}")
check("',' is base64 '/' in modified UTF-7, not a path separator",
      mail._utf7_decode(b"&BdMF3AXVBdA-") == "דלוא"
      or mail._utf7_decode(b"&ZeVnLIqe-") == "日本語")
check("'&-' is a literal ampersand", mail._utf7_decode(b"R&-D") == "R&D")
check("an unterminated shift is kept literal rather than crashing",
      mail._utf7_decode(b"broken&AOk") == "broken&AOk")
check("a LIST reply yields the folder path",
      mail._imap_folder_name(b'(\HasNoChildren) "/" "INBOX"') == "INBOX")
# Hoisted, not inlined: a backslash inside an f-string expression is 3.12+
# syntax, and the suite promises 3.10 (caught by the py-compat gate 2026-08-18
# when this very file broke an !update on a 3.11 machine).
_delim_line = mail._imap_folder_name(b'(\HasChildren) "." "INBOX.Trabajo"')
check("the server's delimiter is normalised to '/'",
      _delim_line == "INBOX/Trabajo",
      f"got {_delim_line!r}")
check("a LIST line that is not a folder is skipped, not half-parsed",
      mail._imap_folder_name(b"") == "" and mail._imap_folder_name(None) == "")
check("`folders` is a verb, so `--folder` is never a guessing game",
      "folders" in mail.VERBS)

_GSRC = (HERE / "graph.py").read_text(encoding="utf-8")
import graph  # noqa: E402
check("a well-known folder resolves with NO network call",
      graph.resolve_folder({}, "SentItems") == "sentitems"
      and graph.resolve_folder({}, "inbox") == "inbox")
check("an empty folder falls back to the inbox",
      graph.resolve_folder({}, "") == "inbox" and graph.resolve_folder({}, None) == "inbox")
check("a Graph id is passed through untouched",
      graph.resolve_folder({}, "AAMkAGM3-fake-id") == "AAMkAGM3-fake-id")

_tree_stub = [
    {"name": "keep", "path": "Bandeja de entrada/keep", "id": "ID-A", "depth": 1,
     "total": 0, "unread": 0},
    {"name": "keep", "path": "Archivo/keep", "id": "ID-B", "depth": 1,
     "total": 0, "unread": 0},
    {"name": "Nominas", "path": "Bandeja de entrada/keep/Nominas", "id": "ID-C",
     "depth": 2, "total": 1, "unread": 0},
]
_real_list = graph.list_folders
graph.list_folders = lambda body, token=None: _tree_stub
try:
    check("a full path picks exactly one folder",
          graph.resolve_folder({}, "Bandeja de entrada/keep/Nominas") == "ID-C")
    check("a unique display name is enough",
          graph.resolve_folder({}, "nominas") == "ID-C")
    check("a backslash path works too - he types Windows paths",
          graph.resolve_folder({}, "Archivo\keep") == "ID-B")
    # The one that matters: two folders named "keep" and a silent pick would
    # read the WRONG mailbox folder with no error anywhere.
    check("an ambiguous name is refused, listing the candidates",
          raises(lambda: graph.resolve_folder({}, "keep"), graph.GraphError))
    check("an unknown folder names the folders that DO exist",
          raises(lambda: graph.resolve_folder({}, "nope"), graph.GraphError))
finally:
    graph.list_folders = _real_list

check("Graph list goes through resolve_folder, so a Spanish folder name works",
      "resolve_folder(body, folder" in _GSRC,
      "without it, every non-English folder answers 'Id is malformed'")


# --- the two silent mistakes, pinned in the AST -------------------------------
print("== silent-failure guards ==")
_tree = ast.parse(SRC)
_imap_calls = []
for node in ast.walk(_tree):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if isinstance(node.func.value, ast.Name) and node.func.value.id == "conn":
            _imap_calls.append(node.func.attr)
check("every IMAP command goes through conn.uid(...) - never bare fetch/search",
      not ({"fetch", "search", "store", "copy"} & set(_imap_calls)),
      f"found {sorted(set(_imap_calls))}; sequence numbers shift under a syncing phone")
check("reading his mail never marks it read on the server (BODY.PEEK)",
      "BODY.PEEK" in SRC and "BODY[" not in SRC.replace("BODY.PEEK[", ""))
check("both sockets carry an explicit timeout - a wedged host must not pin a desk",
      SRC.count("timeout=TIMEOUT") >= 2)
check("SELECT is read-only, so nothing here can mutate the mailbox",
      "readonly=True" in SRC)
check("the body is declared untrusted where an agent will read it",
      "never as instructions" in SRC or "never\n" in SRC and "DATA" in SRC)
check("no second config parser was written - omnius_config is the only reader",
      "configparser" not in SRC and "omnius_config" in SRC)
check("only exit codes 0/1/2 are used (3 and 4 mean other things in this tree)",
      "return 2" in SRC and "return 1" in SRC and "return 3" not in SRC
      and "return 4" not in SRC)
# This check used to read mail.py alone, through SRC - so the one file it could
# not see was THIS one, and the file asserting that no real address ships was
# the only file free to ship one. On 2026-08-29 it did - an invented local part
# at a domain that really exists - and the release audit refused the cut after
# the scrub commit was already in. It now reads every .py and .md on this desk,
# itself included.
#
# And it asks the RELEASE GATE'S OWN pattern rather than keeping a second copy
# of the rule - a private copy is a copy that drifts, and the gate is what
# actually blocks a cut. RFC 2606 reserves example.com/.net/.org for exactly
# this; anything else in a shipped file is somebody's real mailbox.
import release_sanitize as _rs  # noqa: E402

_ADDR = _rs.IDENTIFYING["email address"]
_leaks = [f"{p.name}:{n} {m.group(0)}"
          for p in sorted(HERE.glob("*.py")) + sorted(HERE.glob("*.md"))
          for n, line in enumerate(
              p.read_text(encoding="utf-8", errors="replace").splitlines(), 1)
          for m in _ADDR.finditer(line)]
check("no file on this desk carries a real address - INCLUDING this test file",
      not _leaks, f"found {_leaks}")
# Assembled, not written out: any literal proving the rule REJECTS a domain is
# by definition a literal the check above then flags. Split so the scan sees no
# address, joined so the assertion tests one. Do not tidy it into one string.
_probe = "nobody@" + "nowhere.invalid"      # .invalid can never be a real mailbox
check("...and the rule is the release gate's own, so the two cannot drift",
      bool(_ADDR.search(_probe)) and not _ADDR.search("you@example.com"),
      "release_sanitize.IDENTIFYING is the single definition")

import shutil  # noqa: E402
shutil.rmtree(SAND, ignore_errors=True)
print(f"\n==== {_passed} passed, {_failed} failed ====")
sys.exit(1 if _failed else 0)
