# Notes

A minimal personal notes + tasks app: **one Python file, stdlib only, no
database, no build step**. The markdown files in the notes folder ARE the
product — any editor, script, or AI agent pointed at that folder can read
everything without this app existing. The app is a VS Code-dark UI over
them: each note is a card, day headers stick while you scroll.

## For AI agents (start here)

If you were just connected to this workspace, here's everything you need.

**What this is.** A personal notes + tasks app. Everything the user records
lives as plain markdown in the `notes/` folder — one file per month, e.g.
`notes/2026-07.md`. Those files ARE the data; the Python app is just a UI over
them. You do not need the app running to read or answer questions about the
notes — just read the month files. The exact file format is defined below
under "Where notes live & file format". A **note** is a line like
`- HH:MM text`; a **task** is the same line with a checkbox: `- [ ] HH:MM text`
(open) or `- [x] HH:MM text` (done).

**To answer questions about the notes:** read the month files in `notes/`
directly. If the server is running (http://localhost:5111) you can instead use
the read endpoints under "API" (`GET /api/month/...`, `GET /api/search?...`)
which return JSON.

**To create a note or task, pick one path:**

- **Preferred — via the API, only when the server is running.** `POST
  /api/note` with `{"text": "...", "task": false}` (set `"task": true` for an
  open task). The app adds the timestamp, keeps writes append-only, creates the
  day header if needed, and swaps the file atomically. This is the safe path.
- **Direct file edit — fine only when the server is NOT running.** Append to
  the current month file and match the format exactly:
  - Add a `## YYYY-MM-DD Ddd` day header first if today isn't in the file yet
    (`Ddd` = Mon/Tue/Wed/Thu/Fri/Sat/Sun).
  - Write the whole item yourself, **including the `HH:MM ` time prefix** —
    the app only auto-adds that via the API, so if you edit the file you must
    add it: `- HH:MM text` (note), `- [ ] HH:MM text` (open task),
    `- [x] HH:MM text` (done task). Time is 24h in the user's local timezone.
  - Append only — never rewrite or reorder existing lines.

**Do not** edit month files directly while the server is running: you'd bypass
the content-hash conflict check and could clobber a line. Use the API then.

## Install

There is nothing to install — Notes has **no third-party dependencies**, so
`requirements.txt` is deliberately empty of packages. On a fresh machine,
double-click **`install.bat`**: it checks that Python 3.10+ is on PATH,
confirms `app.py` loads, creates the notes folder, and runs the self-test
so you know the copy is good before you trust it with anything. It writes
nothing outside the project and never touches existing notes.

## Run

Double-click **`run.bat`** — it starts the server and opens the browser
(and if Notes is already running, it just opens the browser). Or manually:

```
python app.py
```

Open http://localhost:5111. Requires Python 3.10+ (standard library only).
By default the server binds to localhost (127.0.0.1) only — see
"Reaching it from another device" below to change that.

## Configuration

Settings live in **`..\config\notes.ini`** — the workspace-wide config folder,
so there is one place to look rather than one per tool (see `config\README.md`).
Running this app on its own, outside an Omnius tree? It still falls back to a
`config.ini` next to `app.py`, and `NOTES_CONFIG=<path>` overrides both.

Every key is optional and ships commented out, so an untouched file behaves
exactly like no file at all — uncomment a line to change something:

```ini
[notes]
# notes_dir = notes
# port = 5111
# host = 127.0.0.1
```

| Key         | Env var      | Default                   | Meaning                          |
| ----------- | ------------ | ------------------------- | -------------------------------- |
| `notes_dir` | `NOTES_DIR`  | `notes/` next to `app.py` | where the files live             |
| `port`      | `PORT`       | `5111`                    | server port                      |
| `host`      | `NOTES_HOST` | `127.0.0.1`               | bind address (this machine only) |

**Precedence: environment variable > `config.ini` > default.** The env vars
are the one-off override — `set NOTES_HOST=0.0.0.0 && python app.py` wins
for that run without editing anything. `NOTES_CONFIG` points at a different
config file. A relative `notes_dir` is resolved against the folder holding
`app.py`, not the folder you launch from; a malformed config file or an
unparseable port is reported and ignored rather than fatal.

## Reaching it from another device

Set `host = 0.0.0.0` in `config.ini` to serve on every network interface,
so another PC or a phone on the same Wi-Fi can open
`http://<this-machine-ip>:5111`.

**There is no authentication.** Anyone who can reach that address gets
everything: they can read every note, and the API accepts their writes,
edits and deletes (`POST /api/note`, `/api/edit`, `/api/delete`). Treat it
the way you'd treat leaving the notes open on an unlocked screen — fine on
a home network you control, not on a café, office, or guest Wi-Fi.

The server prints a warning at startup, naming the reachable URL and where
the setting came from, whenever `host` is not a loopback address. Set it
back to `127.0.0.1` to go private again.

## Pages

- **Today** (`#/`) — dashboard: quick-add box, all open tasks across every
  month, and today's notes.
- **Notes** (`#/notes`) — the archive: month navigation, live search across
  all months (matches highlighted), day filter, kind filter (notes / tasks /
  open / done), sort order (newest first by default, switchable to oldest
  first — remembered across sessions), per-note actions (edit in place,
  convert, delete) and bulk delete.
- **Stats** (`#/stats`) — counts (notes, tasks, open, done), a current
  writing streak, a 90-day activity grid, totals per month, and your most
  used `#tags` as chips that jump straight to that search. All computed
  server-side in one pass, so the page is one request rather than one per
  month.
- **Settings** (`#/settings`) — every Omnius setting in effect, **and where
  each value came from** (environment, config file, or default), plus which
  secrets are set and which mail accounts and AI capabilities are ready.
  **Read-only, and values of secrets are never shown** — only `set` /
  `NOT SET`. Outside an Omnius workspace the page simply says so; this app
  still runs on its own.
- **Write** (`#/new`) — full composer, GitHub-issue style: Write/Preview
  tabs, formatting buttons (bold, italic, strikethrough, code, code block,
  quote, list), paste or drag-and-drop attachments, save-as-task toggle.
  Ctrl+Enter saves. Drafts are kept in the browser, so navigating away
  never loses a half-written note.

Notes render with full GitHub-flavored markdown: tables, fenced code blocks
(with syntax highlighting and a copy button), blockquotes, headings,
nested / ordered / task lists, strikethrough, links, images, and bare-URL
autolinks. `#tags` are clickable and jump straight to a search.

## Where notes live & file format

One markdown file per month: `notes/2026-07.md`. Exact format:

```markdown
# 2026-07

## 2026-07-15 Wed

- 09:42 note text here
- [ ] 10:10 an open task
- [x] 10:40 a finished task
- 11:05 another note, markdown allowed: **bold**, `code`, [links](url)
```

- The month file starts with a `# YYYY-MM` title.
- Each day is a `## YYYY-MM-DD Ddd` header (`Ddd` = English weekday
  abbreviation: Mon/Tue/Wed/Thu/Fri/Sat/Sun).
- A note is one markdown list item: `- HH:MM text` (24h local time). The
  timestamp prefix is added by the app; you only type the text.
- A **task** is the same item with a GitHub-flavored checkbox in front of
  the time: `- [ ] HH:MM text` (open) or `- [x] HH:MM text` (done) — so
  tasks render as native checkboxes anywhere GFM does.
- Marking a task done appends **when** it was completed, using the
  Obsidian-compatible marker: `- [x] 10:05 text ✅ 2026-07-15 14:32`.
  Unchecking removes the stamp; completing again writes a fresh one. Parsed
  notes expose it as the `completed` field, and the UI shows it as a small
  green "✓ completed …" line under the task.
- **Multi-line notes**: continuation lines are indented with two spaces, so
  the whole note remains a single markdown list item.
- Files are UTF-8 with `\n` line endings.
- Note text is sanitized minimally on write: U+0000 becomes U+FFFD, and
  every Unicode line separator (U+2028, form feed, ...) is treated as a
  newline — so a note can never forge a day header inside the file.

## Attachments

Paste or drop files into the composer (or quick-add box). They are stored
under `notes/files/YYYY-MM/` with a sanitized, timestamped name, and the
markdown gets a relative link — `![shot.png](files/2026-07/15-104233-shot.png)`
for images, `[report.pdf](files/...)` for everything else. Because the links
are relative to the notes folder, external markdown renderers resolve them
too. The app serves them at `/files/...` (with a sandbox CSP, so nothing
uploaded can ever run script).

## Durability & mutations

Writing notes is **append-only** — saving never rewrites existing content.
Four explicit actions mutate a file, each rewriting only the affected
lines and swapping the file in atomically:

- toggling a task's checkbox (`[ ]` ↔ `[x]`),
- converting a note to a task and back,
- editing a note's text in place (timestamp and kind are kept),
- deleting notes (individually or in bulk).

Every mutation carries the note's content hash; if the file changed on disk
in the meantime the API answers `409` instead of touching the wrong line.
**Deleted notes are appended to `notes/.trash.md`** with a timestamp and
their original date, so nothing is ever silently lost. A day header whose
last note was deleted is pruned; an emptied month file is removed.

## API

All endpoints return JSON, so scripts and agents can use the app too.
Mutation endpoints identify a note by its `line` (block start line in the
month file) and `sha` (short content hash) — both returned by every read
endpoint. On hash mismatch they return `409`.

| Method & path            | Body / params          | Returns |
| ------------------------ | ---------------------- | ------- |
| `POST /api/note`         | `{"text": "...", "task": false}` | `201` + `{"ok": true, "note": {...}}` |
| `GET /api/month/YYYY-MM` | —                      | `{"month", "days": [{"date", "weekday", "notes": [{"time", "text", "type", "done", "line", "sha"}]}]}` — days newest first |
| `GET /api/search?q=...&type=all` | `type` ∈ all/note/task/open/done; `q` optional when a type filter is given | `{"query", "type", "results": [...]}` — across all months, newest date first |
| `GET /api/days?month=YYYY-MM` | month optional (defaults to current) | `{"month", "days": [{"date", "weekday", "count"}]}` |
| `GET /api/months`        | —                      | `{"months": [...]}` — every month file that exists |
| `POST /api/task`         | `{"month", "line", "sha", "done": true}` | toggles a task's done state; returns `completed` (the `✅` timestamp, or `null` when unchecking) |
| `POST /api/convert`      | `{"month", "line", "sha", "to": "task"\|"note"}` | converts note ↔ task |
| `POST /api/edit`         | `{"month", "line", "sha", "text": "..."}` | replaces a note's text, keeping its time and kind |
| `POST /api/delete`       | `{"month", "notes": [{"line", "sha"}, ...]}` | deletes 1–500 notes, logs them to `.trash.md` |
| `POST /api/upload?name=file.png` | raw file bytes as body (≤ 25 MB) | `201` + `{"path", "markdown", "bytes"}` |
| `GET /files/<path>`      | —                      | the stored attachment |

Validation: empty/whitespace-only notes and malformed month strings are
rejected with `400` and `{"error": "..."}`. Notes are capped at 64 KB,
uploads at 25 MB.

Examples:

```
curl -X POST http://localhost:5111/api/note -d "{\"text\": \"ship the demo #linkbox\", \"task\": true}"
curl "http://localhost:5111/api/search?q=&type=open"
curl -X POST "http://localhost:5111/api/upload?name=shot.png" --data-binary @shot.png
```

## Keyboard

- Quick-add (Today): **Enter** saves, **Shift+Enter** newline.
- Write page: **Ctrl+Enter** saves, Enter is a normal newline.
- Editing a note: **Ctrl+Enter** saves, **Esc** cancels.
- **/** opens search (jumps to the Notes page), **Esc** clears it.
- **n** jumps to the Write page, **t** jumps to Today.

## Tests

```
python test_storage.py
```

118 checks: byte-exact file format, append-only across day/month boundaries,
task toggle/convert/edit (line-surgical rewrites), delete + trash +
day-header pruning, kind-filtered search, upload sanitization and
path-traversal refusal, config-file precedence and graceful handling of a
broken one, and a smoke test of every API endpoint including validation
errors, 409 conflicts, and keep-alive behavior.
