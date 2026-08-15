"""Quick verification of the storage layer and the JSON API.

Run: python test_storage.py
Uses a temporary notes folder; never touches your real notes.
"""

import http.client
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

import app

passed = 0


def ok(cond, label):
    global passed
    assert cond, "FAILED: " + label
    passed += 1
    print("  ok  " + label)


def dt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M")


def month_text(month):
    return (app.NOTES_DIR / (month + ".md")).read_text(encoding="utf-8")


def flat(month):
    return [n for d in app.parse_month(month)["days"] for n in d["notes"]]


tmp = tempfile.TemporaryDirectory()
app.NOTES_DIR = Path(tmp.name) / "notes"

# ---------------------------------------------------------------- format
print("storage: exact file format")

ok(app.WEEKDAYS[datetime(2026, 7, 15).weekday()] == "Wed",
   "weekday mapping (2026-07-15 is a Wednesday)")

app.append_note("note text here", dt("2026-07-15 09:42"))
app.append_note("another note, markdown allowed: **bold**, `code`, [links](url)",
                dt("2026-07-15 11:05"))
expected = (
    "# 2026-07\n"
    "\n"
    "## 2026-07-15 Wed\n"
    "\n"
    "- 09:42 note text here\n"
    "- 11:05 another note, markdown allowed: **bold**, `code`, [links](url)\n"
)
ok(month_text("2026-07") == expected, "month file matches the spec format byte for byte")

# ---------------------------------------------------------------- append-only
print("storage: append-only across day and month boundaries")

before = (app.NOTES_DIR / "2026-07.md").read_bytes()
app.append_note("first note next day", dt("2026-07-16 08:00"))
after = (app.NOTES_DIR / "2026-07.md").read_bytes()
ok(after.startswith(before), "day boundary: existing bytes unchanged")
ok(after.decode("utf-8").endswith(
    "\n\n## 2026-07-16 Thu\n\n- 08:00 first note next day\n"),
   "day boundary: new day header appended in place")

before7 = (app.NOTES_DIR / "2026-07.md").read_bytes()
note = app.append_note("hello august", dt("2026-08-01 00:01"))
ok(month_text("2026-08") ==
   "# 2026-08\n\n## 2026-08-01 Sat\n\n- 00:01 hello august\n",
   "month boundary: new file created with # YYYY-MM title")
ok((app.NOTES_DIR / "2026-07.md").read_bytes() == before7,
   "month boundary: previous month file untouched")
ok(note == {"month": "2026-08", "date": "2026-08-01", "weekday": "Sat",
            "time": "00:01", "text": "hello august", "type": "note", "done": False},
   "append_note returns the saved note")

# ---------------------------------------------------------------- multi-line
print("storage: multi-line notes")

text = "shopping list\n- milk\n- eggs\n\ncall @bob about #project"
before8 = (app.NOTES_DIR / "2026-08.md").read_bytes()
app.append_note(text, dt("2026-08-01 07:30"))
after8 = (app.NOTES_DIR / "2026-08.md").read_bytes()
ok(after8.startswith(before8), "multi-line append: existing bytes unchanged")
ok(after8.decode("utf-8").endswith(
    "- 07:30 shopping list\n  - milk\n  - eggs\n\n  call @bob about #project\n"),
   "continuation lines indented two spaces (stays one markdown list item)")
ok(flat("2026-08")[-1]["text"] == text, "multi-line note round-trips through parse")

# ---------------------------------------------------------------- tasks
print("storage: tasks")

t = app.append_note("call the tax office", dt("2026-08-01 09:00"), task=True)
ok(t["type"] == "task" and t["done"] is False, "append_note(task=True) returns a task")
ok(month_text("2026-08").endswith("- [ ] 09:00 call the tax office\n"),
   "task line uses GitHub checkbox format: - [ ] HH:MM text")

task = flat("2026-08")[-1]
ok(task["type"] == "task" and task["done"] is False and
   task["text"] == "call the tax office" and "sha" in task,
   "task parses with type/done/line/sha")

before_t = month_text("2026-08")
app.set_task_done("2026-08", task["line"], task["sha"], True)
after_t = month_text("2026-08")
ok("- [x] 09:00 call the tax office" in after_t, "set_task_done writes [x]")
ok([ln for ln in before_t.split("\n") if "09:00" not in ln] ==
   [ln for ln in after_t.split("\n") if "09:00" not in ln],
   "toggling rewrites only the task's own line")
ok(flat("2026-08")[-1]["done"] is True, "parse sees the task as done")

task = flat("2026-08")[-1]
try:
    app.set_task_done("2026-08", task["line"], "wrong-sha", False)
    ok(False, "stale sha accepted")
except app.Conflict:
    ok(True, "stale sha raises Conflict (409)")

app.set_task_done("2026-08", task["line"], task["sha"], False)
ok("- [ ] 09:00 call the tax office" in month_text("2026-08"), "toggle back to open")

# completion timestamps
task = flat("2026-08")[-1]
res = app.set_task_done("2026-08", task["line"], task["sha"], True,
                        now=dt("2026-08-02 16:45"))
ok(res["completed"] == "2026-08-02 16:45" and
   "- [x] 09:00 call the tax office ✅ 2026-08-02 16:45" in month_text("2026-08"),
   "completing a task stamps the line with ✅ datetime")
task = flat("2026-08")[-1]
ok(task["done"] is True and task["completed"] == "2026-08-02 16:45" and
   task["text"] == "call the tax office",
   "parse extracts the stamp into 'completed' and keeps the text clean")
res = app.set_task_done("2026-08", task["line"], task["sha"], True,
                        now=dt("2026-08-03 08:00"))
ok(month_text("2026-08").count("✅") == 1 and res["completed"] == "2026-08-03 08:00",
   "re-stamping replaces the old stamp instead of stacking")
task = flat("2026-08")[-1]
app.convert_note("2026-08", task["line"], task["sha"], "note")
ok("✅" not in month_text("2026-08") and
   "- 09:00 call the tax office" in month_text("2026-08"),
   "converting a done task to a note strips checkbox and stamp")
n = flat("2026-08")[-1]
app.convert_note("2026-08", n["line"], n["sha"], "task")
task = flat("2026-08")[-1]
res = app.set_task_done("2026-08", task["line"], task["sha"], True,
                        now=dt("2026-08-03 09:00"))
res = app.set_task_done("2026-08", flat("2026-08")[-1]["line"],
                        flat("2026-08")[-1]["sha"], False)
ok(res["completed"] is None and "✅" not in month_text("2026-08") and
   "- [ ] 09:00 call the tax office" in month_text("2026-08"),
   "unchecking removes the stamp")

# ---------------------------------------------------------------- convert
print("storage: convert note <-> task")

first = flat("2026-08")[0]
ok(first["text"] == "hello august", "target located")
app.convert_note("2026-08", first["line"], first["sha"], "task")
ok("- [ ] 00:01 hello august" in month_text("2026-08"), "note converts to open task")
first = flat("2026-08")[0]
app.convert_note("2026-08", first["line"], first["sha"], "note")
ok("- 00:01 hello august" in month_text("2026-08"), "task converts back to note")
first = flat("2026-08")[0]
try:
    app.convert_note("2026-08", first["line"], first["sha"], "note")
    ok(False, "double convert accepted")
except ValueError:
    ok(True, "converting a note to a note is rejected")

# ---------------------------------------------------------------- edit
print("storage: edit in place")

target = [n for n in flat("2026-08") if n["text"] == "hello august"][0]
app.edit_note("2026-08", target["line"], target["sha"], "hello edited august")
ok("- 00:01 hello edited august" in month_text("2026-08"),
   "edit_note replaces the text, keeps time and kind")

target = [n for n in flat("2026-08") if n["text"] == "hello edited august"][0]
app.edit_note("2026-08", target["line"], target["sha"], "multi\nline\n\nedit")
ok("- 00:01 multi\n  line\n\n  edit\n" in month_text("2026-08"),
   "edit_note writes continuation lines with two-space indents")
ok(flat("2026-08")[0]["text"] == "multi\nline\n\nedit",
   "edited multi-line note round-trips through parse")

target = flat("2026-08")[0]
try:
    app.edit_note("2026-08", target["line"], "stale-sha-here", "nope")
    ok(False, "stale sha accepted on edit")
except app.Conflict:
    ok(True, "edit with stale sha raises Conflict (409)")

tsk = [n for n in flat("2026-08") if n["type"] == "task"][0]
was_done = tsk["done"]
app.edit_note("2026-08", tsk["line"], tsk["sha"], "edited task text")
tsk2 = [n for n in flat("2026-08") if n["text"] == "edited task text"][0]
ok(tsk2["type"] == "task" and tsk2["done"] == was_done,
   "editing a task keeps its checkbox state")

app.set_task_done("2026-08", tsk2["line"], tsk2["sha"], True,
                  now=dt("2026-08-04 17:20"))
tsk3 = [n for n in flat("2026-08") if n["text"] == "edited task text"][0]
app.edit_note("2026-08", tsk3["line"], tsk3["sha"], "edited again after done")
tsk4 = [n for n in flat("2026-08") if n["text"] == "edited again after done"][0]
ok(tsk4["done"] is True and tsk4["completed"] == "2026-08-04 17:20" and
   "edited again after done ✅ 2026-08-04 17:20" in month_text("2026-08"),
   "editing a completed task preserves its completion stamp")
app.set_task_done("2026-08", tsk4["line"], tsk4["sha"], False)

try:
    app.edit_note("2026-08", target["line"], target["sha"], "  \n ")
    ok(False, "empty edit accepted")
except ValueError:
    ok(True, "empty replacement text is rejected")

# ---------------------------------------------------------------- delete
print("storage: delete + trash")

app.append_note("delete me", dt("2026-09-01 10:00"))
app.append_note("keep me\nwith a second line", dt("2026-09-01 10:05"))
app.append_note("day two note", dt("2026-09-02 11:00"))

victim = [n for n in flat("2026-09") if n["text"] == "delete me"][0]
n_del = app.delete_notes("2026-09", [{"line": victim["line"], "sha": victim["sha"]}],
                         now=dt("2026-09-03 08:00"))
ok(n_del == 1 and month_text("2026-09") ==
   "# 2026-09\n\n## 2026-09-01 Tue\n\n- 10:05 keep me\n  with a second line\n"
   "\n## 2026-09-02 Wed\n\n- 11:00 day two note\n",
   "single delete removes exactly that block")
trash = (app.NOTES_DIR / ".trash.md").read_text(encoding="utf-8")
ok("deleted from 2026-09.md" in trash and "- 10:00 delete me" in trash,
   "deleted block is preserved in notes/.trash.md")

day2 = [n for n in flat("2026-09") if n["text"] == "day two note"][0]
app.delete_notes("2026-09", [{"line": day2["line"], "sha": day2["sha"]}])
ok("## 2026-09-02" not in month_text("2026-09"),
   "day header is pruned when its last note is deleted")

rest = flat("2026-09")
app.delete_notes("2026-09", [{"line": n["line"], "sha": n["sha"]} for n in rest])
ok(not (app.NOTES_DIR / "2026-09.md").exists() and "2026-09" not in app.list_months(),
   "empty month file is removed after deleting everything")

try:
    app.delete_notes("2026-08", [{"line": 0, "sha": "nope"}])
    ok(False, "bad delete accepted")
except LookupError:
    ok(True, "delete with unknown line -> LookupError (404)")

# bulk across two days in one call
app.append_note("bulk a", dt("2026-09-10 09:00"))
app.append_note("bulk b", dt("2026-09-11 09:30"))
app.append_note("bulk survivor", dt("2026-09-11 09:45"))
refs = [{"line": n["line"], "sha": n["sha"]}
        for n in flat("2026-09") if n["text"] in ("bulk a", "bulk b")]
ok(app.delete_notes("2026-09", refs) == 2 and
   [n["text"] for n in flat("2026-09")] == ["bulk survivor"],
   "bulk delete removes several notes in one call")

# ---------------------------------------------------------------- parse
print("storage: parse")

data = app.parse_month("2026-07")
ok([d["date"] for d in data["days"]] == ["2026-07-16", "2026-07-15"],
   "days come back newest first")
n0 = data["days"][1]["notes"][0]
ok(data["days"][1]["weekday"] == "Wed" and n0["time"] == "09:42" and
   n0["text"] == "note text here" and n0["type"] == "note",
   "day header and note lines parse into time + text")
ok(app.parse_month("2030-01") == {"month": "2030-01", "days": []},
   "missing month parses to empty (graceful empty state)")
ok(app.list_months() == ["2026-07", "2026-08", "2026-09"],
   "list_months finds every month file, sorted")
ok(app.day_counts("2026-07") == [
    {"date": "2026-07-16", "weekday": "Thu", "count": 1},
    {"date": "2026-07-15", "weekday": "Wed", "count": 2}],
   "day_counts")

# ---------------------------------------------------------------- search
print("storage: search + kind filters")

hits = app.search_notes("NOTE")
ok([(h["date"], h["time"]) for h in hits][:3] ==
   [("2026-07-16", "08:00"), ("2026-07-15", "09:42"), ("2026-07-15", "11:05")],
   "search is case-insensitive, spans months, newest date first")
ok(app.search_notes("#project")[0]["date"] == "2026-08-01",
   "search matches inside multi-line notes")
ok(app.search_notes("nothing-matches-this") == [], "no matches -> empty list")

app.append_note("open task probe", dt("2026-08-05 08:00"), task=True)
opens = app.search_notes("", "open")
ok(all(n["type"] == "task" and not n["done"] for n in opens) and
   any(n["text"] == "open task probe" for n in opens),
   "empty query + kind=open lists all open tasks")
probe = [n for n in opens if n["text"] == "open task probe"][0]
app.set_task_done("2026-08", probe["line"], probe["sha"], True)
ok(all(n["text"] != "open task probe" for n in app.search_notes("", "open")) and
   any(n["text"] == "open task probe" for n in app.search_notes("", "done")),
   "done task moves from kind=open to kind=done")
ok(all(n["type"] == "note" for n in app.search_notes("", "note")),
   "kind=note excludes tasks")

# ---------------------------------------------------------------- hardening
print("storage: hardening")

annotated = app.NOTES_DIR / "2026-05.md"
annotated.write_text(
    "# 2026-05\n\n## 2026-05-01 Fri\n\n- 09:00 friday note\n"
    "\n## 2026-05-02 Sat (vacation)\n\n- 10:00 saturday note\n",
    encoding="utf-8")
parsed = app.parse_month("2026-05")
ok([(d["date"], len(d["notes"])) for d in parsed["days"]] ==
   [("2026-05-02", 1), ("2026-05-01", 1)],
   "hand-annotated day header keeps its notes on the right date")

app.append_note("evil\u2028## 2099-01-01 Thu", dt("2026-08-06 09:00"))
raw = month_text("2026-08")
ok("\u2028" not in raw and "\n  ## 2099-01-01 Thu\n" in raw,
   "U+2028 becomes a real line break; forged header ends up indented")
before = (app.NOTES_DIR / "2026-08.md").read_bytes()
app.append_note("follow-up same day", dt("2026-08-06 09:05"))
after = (app.NOTES_DIR / "2026-08.md").read_bytes()
ok(after.startswith(before) and
   after.decode("utf-8").count("## 2026-08-06") == 1,
   "no duplicate day header after separator-injection attempt")

note = app.append_note("nul \x00 byte", dt("2026-08-06 09:10"))
ok("\x00" not in month_text("2026-08") and note["text"] == "nul � byte",
   "NUL is replaced with U+FFFD before storage")

for bad in ("", "   ", "\n \n"):
    try:
        app.append_note(bad)
        ok(False, "empty note accepted")
    except ValueError:
        pass
ok(True, "empty / whitespace-only notes are rejected")

# ---------------------------------------------------------------- uploads
print("storage: uploads")

up = app.save_upload("Screen Shot (1).PNG", b"\x89PNG fake", now=dt("2026-08-06 10:00"))
ok(up["path"].startswith("files/2026-08/06-100000-") and
   up["path"].endswith("-Screen-Shot-1.png") and "(" not in up["path"],
   "filename is sanitized and stamped")
ok(up["markdown"].startswith("![") and up["path"] in up["markdown"],
   "image uploads produce image markdown")
ok((app.NOTES_DIR / up["path"]).read_bytes() == b"\x89PNG fake",
   "upload bytes land under notes/files/")

doc = app.save_upload("report.pdf", b"%PDF fake", now=dt("2026-08-06 10:01"))
ok(doc["markdown"].startswith("[report.pdf]("), "non-image uploads produce link markdown")

dup = app.save_upload("report.pdf", b"%PDF fake 2", now=dt("2026-08-06 10:01"))
ok(dup["path"] != doc["path"], "same-name uploads never collide")

ok(app.resolve_upload("../2026-08.md") is None and
   app.resolve_upload("/etc/passwd") is None and
   app.resolve_upload("2026-08\\evil") is None,
   "path traversal out of files/ is refused")
ok(app.resolve_upload(up["path"][len("files/"):]) is not None,
   "legitimate upload path resolves")

ok(app.list_months() == ["2026-05", "2026-07", "2026-08", "2026-09"],
   "files/ and .trash.md never appear as months")

# ------------------------------------------------- v2 review regressions
print("storage: v2 review regressions")

app.append_note("standup", dt("2026-10-01 09:00"), task=True)
app.append_note("standup", dt("2026-10-02 09:00"), task=True)
notes10 = flat("2026-10")
d2, d1 = notes10[0], notes10[1]
ok(d1["sha"] != d2["sha"], "identical blocks on different days get different shas")
stale = {"line": d1["line"], "sha": d1["sha"]}
app.delete_notes("2026-10", [dict(stale)])
shifted = flat("2026-10")[0]
ok(shifted["line"] == stale["line"],
   "precondition: day-2's identical note shifted onto the stale line")
try:
    app.set_task_done("2026-10", stale["line"], stale["sha"], True)
    ok(False, "stale ref mutated the wrong day's note")
except (app.Conflict, LookupError):
    ok(True, "stale ref is rejected — sha is date-scoped")
ok(flat("2026-10")[0]["done"] is False, "day-2 task untouched by the stale ref")

p4 = app.NOTES_DIR / "2026-04.md"
p4.write_text("# 2026-04\n\n# reading list\n\n## 2026-04-10 Fri\n\n- 09:00 solo note\n",
              encoding="utf-8")
n = flat("2026-04")[0]
app.delete_notes("2026-04", [{"line": n["line"], "sha": n["sha"]}])
ok(p4.exists() and "# reading list" in p4.read_text(encoding="utf-8"),
   "hand-written heading keeps the month file alive after deleting its last note")

p3 = app.NOTES_DIR / "2026-03.md"
p3.write_text("# 2026-03\n\n## 2026-03-07 Sat (vacation)\n\n- 09:00 beach note\n",
              encoding="utf-8")
n = flat("2026-03")[0]
app.delete_notes("2026-03", [{"line": n["line"], "sha": n["sha"]}],
                 now=dt("2026-03-08 12:00"))
trash3 = (app.NOTES_DIR / ".trash.md").read_text(encoding="utf-8")
ok("### 2026-03-07 Sat (vacation)" in trash3,
   "pruned day-header annotation is preserved in the trash entry")
ok(not p3.exists(), "title-only month file is removed")

p2 = app.NOTES_DIR / "2026-02.md"
p2.write_text("# 2026-02\n\n## 2026-02-01 Sun\n\n## 2026-02-02 Mon\n\n"
              "- 09:00 real note\n- 09:05 second note\n", encoding="utf-8")
n = [x for x in flat("2026-02") if x["text"] == "second note"][0]
app.delete_notes("2026-02", [{"line": n["line"], "sha": n["sha"]}])
ok("## 2026-02-01 Sun" in month_text("2026-02"),
   "hand-made empty day section unrelated to the delete is left alone")

jp = app.save_upload("スクリーンショット.png", b"\x89PNG jp", now=dt("2026-08-06 11:00"))
ok(jp["path"].endswith(".png") and jp["markdown"].startswith("!["),
   "fully non-ASCII filename keeps its extension and image markdown")

errors = []


def _reader():
    try:
        for _ in range(200):
            app.parse_month("2026-08")
            app.list_months()
    except Exception as e:
        errors.append(e)


def _writer():
    try:
        for _ in range(25):
            tsk = [x for x in flat("2026-08") if x["type"] == "task"][0]
            app.set_task_done("2026-08", tsk["line"], tsk["sha"], not tsk["done"])
    except Exception as e:
        errors.append(e)


workers = [threading.Thread(target=_reader) for _ in range(3)]
workers.append(threading.Thread(target=_writer))
for w in workers:
    w.start()
for w in workers:
    w.join()
ok(not errors, "concurrent reads + mutations raise nothing (Windows os.replace safety)")

# ---------------------------------------------------------------- api
print("api: endpoints (ephemeral port, temp notes dir)")

server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
base = "http://127.0.0.1:%d" % port


def get(path):
    try:
        with urllib.request.urlopen(base + path) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def post(path, obj):
    req = urllib.request.Request(base + path, data=json.dumps(obj).encode("utf-8"),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


status, saved = post("/api/note", {"text": "api smoke test #apitest"})
ok(status == 201 and saved["ok"] and saved["note"]["text"] == "api smoke test #apitest",
   "POST /api/note appends and returns the saved note")
month = saved["note"]["month"]

status, saved_task = post("/api/note", {"text": "api task probe", "task": True})
ok(status == 201 and saved_task["note"]["type"] == "task",
   "POST /api/note with task:true appends an open task")

status, data = get("/api/month/" + month)
ok(status == 200 and any(n["text"] == "api smoke test #apitest"
                         for d in data["days"] for n in d["notes"]),
   "GET /api/month/YYYY-MM returns the parsed month")

api_task = [n for d in data["days"] for n in d["notes"] if n["text"] == "api task probe"][0]
status, resp = post("/api/task", {"month": month, "line": api_task["line"],
                                  "sha": api_task["sha"], "done": True})
ok(status == 200 and resp["done"] is True and bool(resp["completed"]),
   "POST /api/task marks a task done and returns the completion timestamp")
ok(post("/api/task", {"month": month, "line": api_task["line"],
                      "sha": "stale0stale0", "done": False})[0] == 409,
   "stale sha -> 409")

status, data = get("/api/month/" + month)
api_task = [n for d in data["days"] for n in d["notes"] if n["text"] == "api task probe"][0]
status, resp = post("/api/convert", {"month": month, "line": api_task["line"],
                                     "sha": api_task["sha"], "to": "note"})
ok(status == 200 and resp["type"] == "note", "POST /api/convert task -> note")

status, data = get("/api/month/" + month)
api_note = [n for d in data["days"] for n in d["notes"] if n["text"] == "api task probe"][0]
status, resp = post("/api/edit", {"month": month, "line": api_note["line"],
                                  "sha": api_note["sha"], "text": "api probe edited"})
ok(status == 200 and resp["text"] == "api probe edited", "POST /api/edit rewrites the text")
ok(post("/api/edit", {"month": month, "line": api_note["line"],
                      "sha": "stale0stale0", "text": "x"})[0] == 409,
   "edit with stale sha -> 409")
ok(post("/api/edit", {"month": month, "line": api_note["line"],
                      "sha": api_note["sha"], "text": "   "})[0] == 400,
   "edit with blank text -> 400")

status, data = get("/api/month/" + month)
api_note = [n for d in data["days"] for n in d["notes"] if n["text"] == "api probe edited"][0]
status, resp = post("/api/delete", {"month": month,
                                    "notes": [{"line": api_note["line"], "sha": api_note["sha"]}]})
ok(status == 200 and resp["deleted"] == 1, "POST /api/delete removes a note")
ok(post("/api/delete", {"month": month, "notes": []})[0] == 400,
   "delete with empty list -> 400")

status, data = get("/api/search?q=%23apitest")
ok(status == 200 and len(data["results"]) == 1, "GET /api/search?q=... finds it")
status, data = get("/api/search?q=&type=open")
ok(status == 200 and all(r["type"] == "task" and not r["done"] for r in data["results"]),
   "GET /api/search?type=open with empty q lists open tasks")
ok(get("/api/search?q=&type=bogus")[0] == 400, "unknown type -> 400")
ok(get("/api/search")[0] == 400, "search without q or type -> 400")

status, data = get("/api/days?month=" + month)
ok(status == 200 and any(d["count"] >= 1 for d in data["days"]), "GET /api/days?month=...")

status, data = get("/api/months")
ok(status == 200 and month in data["months"], "GET /api/months lists month files")

ok(post("/api/note", {"text": "   "})[0] == 400, "empty note -> 400")
ok(post("/api/note", {"wrong": "shape"})[0] == 400, "malformed body -> 400")
ok(get("/api/month/2026-13")[0] == 400, "malformed month -> 400")
ok(get("/api/month/..%2F..%2Fsecrets")[0] == 400, "path-traversal month -> 400")
ok(get("/api/days?month=2026-07%0A")[0] == 400, "month with trailing newline -> 400")

# upload + file serving over HTTP
req = urllib.request.Request(base + "/api/upload?name=pic.png", data=b"\x89PNG http",
                             headers={"Content-Type": "application/octet-stream"},
                             method="POST")
with urllib.request.urlopen(req) as r:
    up = json.loads(r.read())
    up_status = r.status
ok(up_status == 201 and up["markdown"].startswith("!["), "POST /api/upload stores a file")
with urllib.request.urlopen(base + "/" + up["path"]) as r:
    served = r.read()
    csp = r.headers.get("Content-Security-Policy") or ""
ok(served == b"\x89PNG http", "GET /files/... serves the exact bytes back")
ok("sandbox" in csp, "served files carry a sandbox CSP (no script execution)")
try:
    with urllib.request.urlopen(base + "/files/..%2F2026-08.md") as r:
        traversal = r.status
except urllib.error.HTTPError as e:
    traversal = e.code
ok(traversal == 404, "GET /files/../ traversal -> 404")

# keep-alive behavior
conn = http.client.HTTPConnection("127.0.0.1", port)
conn.request("POST", "/api/note",
             body=json.dumps({"text": "keep-alive probe"}).encode("utf-8"),
             headers={"Content-Type": "application/json"})
r1 = conn.getresponse(); r1.read()
conn.request("GET", "/api/months")
r2 = conn.getresponse()
months_ka = json.loads(r2.read())
ok(r1.status == 201 and r2.status == 200 and month in months_ka["months"],
   "happy path keeps the connection alive: POST then GET on one socket")
conn.close()

conn = http.client.HTTPConnection("127.0.0.1", port)
conn.request("POST", "/api/nope", body=b'{"text": "hi"}',
             headers={"Content-Type": "application/json"})
r = conn.getresponse(); r.read()
ok(r.status == 404 and r.getheader("Connection") == "close",
   "POST to wrong path -> 404 + Connection: close (unread body cannot poison keep-alive)")
conn.close()

conn = http.client.HTTPConnection("127.0.0.1", port)
conn.request("POST", "/api/note",
             body=json.dumps({"text": "x" * (70 * 1024)}).encode("utf-8"),
             headers={"Content-Type": "application/json"})
r = conn.getresponse(); r.read()
ok(r.status == 413 and r.getheader("Connection") == "close",
   "oversized note -> 413 + Connection: close")
conn.close()

with urllib.request.urlopen(base + "/") as r:
    html = r.read().decode("utf-8")
ok("<title>Omnius</title>" in html, "GET / serves the page")
ok('href="/favicon.ico"' in html, "...with the emblem as its favicon")
ok('src="/logo.png"' in html, "...and the mark in the header")

# The 2026-08-15 redesign, in the owner's words: "just add/see notes when I am
# at the desk". Four tabs earn their place; Stats and Guide do not - the guide
# duplicates GitHub now the repo is public (a link in Settings remains), and
# the day view IS the statistics that mattered.
for _tab in ("tab-dash", "tab-notes", "tab-new", "tab-settings"):
    ok(f'id="{_tab}"' in html, f"the {_tab.split('-')[1]} tab is present")
ok('id="tab-stats"' not in html and 'id="tab-guide"' not in html,
   "Stats and Guide tabs are gone - four tabs, not six")
ok('id="dayInput"' in html and 'id="dashFleet"' in html,
   "the Today tab ships the day navigator and the fleet section")
ok("GETTING-STARTED.md" in html, "the guide survives as a link in Settings")

# == the day view =============================================================
# "What did I do on day X?" is the question this app exists to answer, and the
# answer is more than notes: inside a workspace, /api/day also assembles the
# fleet's day - commits across every repo, desk activity from the bus
# transcripts. All of it already lived on disk; nothing ever read it as a day.
import subprocess

app.WORKSPACE = ""                                   # force standalone first
app.append_note("day-view note", dt("2026-03-03 09:00"))
app.append_note("day-view task", dt("2026-03-03 10:00"), task=True)
app.append_note("the day after", dt("2026-03-04 08:00"))
_s, _d = get("/api/day?date=2026-03-03")
ok(_s == 200 and _d["date"] == "2026-03-03", "GET /api/day answers")
ok([n["text"] for n in _d["notes"]] == ["day-view note", "day-view task"],
   "...with exactly that day's notes, in file order")
ok(all(k in _d["notes"][0] for k in ("line", "sha", "type", "time")),
   "...as full note objects, so ticks and edits work from the day view")
ok(_d["weekday"] == "Tue", "...and the day header's weekday travels along")
ok(_d["fleet"] is None,
   "standalone: fleet is null, never an error - a bare daybook has no workspace")
_s, _d2 = get("/api/day?date=2026-03-05")
ok(_s == 200 and _d2["notes"] == [], "an empty day is empty, not an error")
_s, _ = get("/api/day?date=03-03-2026")
ok(_s == 400, "a malformed date is refused")

# The fleet half, against a sandbox workspace: one project repo with a commit
# ON the day and one OFF it, plus a desk transcript spanning two days with a
# torn line in the middle.
_ws = Path(tmp.name) / "dayws"
_repo = _ws / "projects" / "demo"
_repo.mkdir(parents=True)


def _git(*args, when=None):
    env = dict(os.environ)
    if when:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
    r = subprocess.run(["git", "-c", "user.email=day@example.com",
                        "-c", "user.name=day", *args],
                       cwd=str(_repo), capture_output=True, timeout=30, env=env)
    assert r.returncode == 0, f"git {args}: {r.stderr.decode(errors='replace')[:200]}"


_git("init", "-q")
(_repo / "a.txt").write_text("1", encoding="utf-8")
_git("add", ".")
_git("commit", "-q", "-m", "on the day", when="2026-03-03T12:30:00")
(_repo / "a.txt").write_text("2", encoding="utf-8")
_git("add", ".")
_git("commit", "-q", "-m", "the day after", when="2026-03-04T09:00:00")
_tdir = _ws / "state" / "transcripts" / "orchestrator"
_tdir.mkdir(parents=True)
(_tdir / "2026-03.jsonl").write_text(
    '{"ts": "2026-03-03T08:00:00Z", "dir": "in", "text": "hi"}\n'
    "not json, a torn line\n"
    '{"ts": "2026-03-03T08:05:00Z", "dir": "out", "text": "hello"}\n'
    '{"ts": "2026-03-04T08:00:00Z", "dir": "in", "text": "other day"}\n',
    encoding="utf-8")
app.WORKSPACE = str(_ws)
_s, _d3 = get("/api/day?date=2026-03-03")
_f = _d3["fleet"]
ok(_f is not None, "inside a workspace the fleet half exists")
ok([c["subject"] for c in _f["commits"]] == ["on the day"],
   "commits: the day's commit and only the day's")
ok(_f["commits"][0]["repo"] == "demo" and _f["commits"][0]["time"] == "12:30",
   "...labelled with its repo and clock time")
ok(_f["desks"] == [{"session": "orchestrator", "in": 1, "out": 1,
                    "first": "08:00", "last": "08:05"}],
   "desk activity: counts and the day's time range, torn line skipped")
app.WORKSPACE = "auto"

with urllib.request.urlopen(base + "/logo.png") as r:
    logo, ctype = r.read(), r.headers.get("Content-Type")
ok(ctype == "image/png" and logo[:8] == b"\x89PNG\r\n\x1a\n", "GET /logo.png serves a real PNG")
# The 1.9 MB original was ~24x the rest of the page; assets\omnius-web.png is a
# 256px copy. A regression here is invisible locally and awful over a phone.
ok(len(logo) < 400_000, f"...and it is the web-sized copy, not the 1.9 MB original ({len(logo)//1024} kB)")

server.shutdown()

# ---------------------------------------------------------------- config
print("\nconfig file")

cfg_dir = Path(tmp.name) / "conf"
cfg_dir.mkdir()


def write_cfg(text):
    p = cfg_dir / "config.ini"
    p.write_text(text, encoding="utf-8")
    return p


ok(app.load_config(cfg_dir / "absent.ini") == {},
   "a missing config file is not an error, just no settings")

cfg = write_cfg("[notes]\nhost = 0.0.0.0\nport = 8080\n")
ok(app.load_config(cfg) == {"host": "0.0.0.0", "port": "8080"},
   "reads the [notes] section")

ok(app.load_config(write_cfg("[other]\nhost = 1.2.3.4\n")) == {},
   "ignores sections that are not [notes]")

ok(app.load_config(write_cfg("[notes]\nhost = 0.0.0.0  # serve the LAN\n"))
   == {"host": "0.0.0.0"}, "strips inline comments")

ok(app.load_config(write_cfg("this is not an ini file at all\n")) == {},
   "a malformed config file is ignored, not fatal")

saved_config, saved_env = app.CONFIG, dict(os.environ)
try:
    app.CONFIG = {"host": "0.0.0.0", "port": "8080"}
    os.environ.pop("NOTES_HOST", None)
    ok(app.setting("host", "NOTES_HOST", "127.0.0.1") == "0.0.0.0",
       "config file overrides the built-in default")

    os.environ["NOTES_HOST"] = "10.0.0.9"
    ok(app.setting("host", "NOTES_HOST", "127.0.0.1") == "10.0.0.9",
       "environment variable overrides the config file")

    app.CONFIG = {}
    os.environ.pop("NOTES_HOST", None)
    ok(app.setting("host", "NOTES_HOST", "127.0.0.1") == "127.0.0.1",
       "falls back to the default when neither is set")

    app.CONFIG = {"port": "not-a-number"}
    ok(app.int_setting("port", "PORT", 5111) == 5111,
       "a non-numeric port falls back to the default instead of crashing")

    app.CONFIG = {"port": " 8080 "}
    ok(app.int_setting("port", "PORT", 5111) == 8080, "numeric port is parsed")
finally:
    app.CONFIG = saved_config
    os.environ.clear()
    os.environ.update(saved_env)

ok(app.is_loopback("127.0.0.1") and app.is_loopback("localhost")
   and app.is_loopback("::1"),
   "loopback addresses are recognised as this-machine-only")
ok(not app.is_loopback("0.0.0.0") and not app.is_loopback("192.168.1.5"),
   "non-loopback addresses are not")

warning = "\n".join(app.exposure_warning("0.0.0.0", 5111))
ok("NO authentication" in warning and "deletes" in warning and ":5111" in warning,
   "the exposure warning names the port and says there is no auth")
ok(warning.isascii(),
   "the warning is ASCII, so it stays legible in any console")

tmp.cleanup()
print("\nall %d checks passed" % passed)
