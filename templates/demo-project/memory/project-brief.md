# linkbox — the brief

**This file is the contract.** `back` and `front` build what it says; the
`auditor` checks what they actually built against it. Where it leaves something
open, the owning desk **decides** and writes the decision into its session
notes — it does not ask, and it does not guess at a sibling's choice.

## What it is

A tiny link-sharing board, running entirely on one machine. Paste a URL, give
it a title and an optional note, see the list newest-first, remove one you no
longer want. That is the whole product.

## Fixed decisions (not open to interpretation)

- **Python standard library only**, both desks. No pip, no framework, no build
  step. This whole workspace runs without a package for a reason.
- **One HTML file**, `front\index.html`, served by back. No bundler, no CDN —
  the page must work with the machine offline.
- **Storage is one JSON file** in `back\`, written atomically (temp file +
  replace). Two posts arriving together must not eat each other.
- **Bind to `127.0.0.1:5117`.** Never `0.0.0.0` — this is a demo on somebody's
  personal PC, not a service.
- Nothing in this project may read `..\..\..\.env`, and no secret, token or
  real email address belongs in any file here.

## The API (back owns the exact shape)

Four operations, and no more:

- list the links, newest first
- add one: `url`, `title`, optional `note`
- delete one by its id
- serve `index.html` at `/`

**back publishes the exact request and response shapes in
`memory\sessions\back.md` BEFORE front builds against them.** front reads those
notes and never reads back's code — a contract you have to reverse-engineer is
not a contract.

## Rules the server must enforce

Server-side, on every write, because a browser check protects nobody:

- **`url` must be `http` or `https`.** Anything else is rejected — `javascript:`
  and `data:` URLs are the interesting ones.
- **`title` at most 120 characters, `note` at most 500.** Both may be any text
  a person can type, in any language.
- **A bad request is a `400` with a one-line reason.** Never a traceback: a
  traceback names absolute paths on the owner's disk, and the auditor will file
  it as a leak.
- An id that does not exist is a `404`, not a crash.

## Rules the page must honour

- **Titles and notes are hostile input.** They are shown as *text*, never as
  markup — a title of `<img src=x onerror=alert(1)>` must appear as those
  characters, in every place it is shown.
- A link's `href` gets checked again in the page. The server promises http/https;
  the page does not take that on trust. Belt and braces is the point of the
  exercise.
- Empty list, failed request, too-long title — each has a visible, plain answer.
  A page that silently does nothing is a bug report waiting to happen.

## Deliberately left open

Decide these, then record the decision:

- ids: counter, uuid, or hash — back's call.
- timestamps: stored how, shown how ("2 hours ago" or a date) — back stores,
  front shows.
- ordering beyond "newest first", and whether deletion asks for confirmation.
- what an empty note looks like: absent field, or empty string.

## Done means

Back: the server runs, every rule above is enforced, and the shapes are written
down in its session notes. Front: the page does all four operations against
those notes. Auditor: a dated file in `memory\audits\` with every finding rated
and pointed at the desk that owns the fix.
