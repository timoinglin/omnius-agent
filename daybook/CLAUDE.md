# CLAUDE.md — the daybook desk

Loaded automatically by every session whose cwd is `daybook\`, including a desk
opened by hand that never runs `/omnius`. The workspace constitution
(`..\CLAUDE.md`) still applies above this file; the bus contract is
`..\.claude\skills\omnius\SKILL.md`, reached via the stub in `.claude\`.

## Read `memory\MEMORY.md` first

This desk runs **`resume: "fresh"`** (`config\fleet.json` → `desks.daybook`,
the stop-gap from `docs\BUGREPORT-2026-08-18-daybook-boot-loop.md`), so **every
run starts with a blank conversation** — no `--continue`, no memory of what the
previous run said or asked.

`memory\MEMORY.md` is where the last run left anything that has to outlive it:
questions already put to the owner and still unanswered, standing agreements
with other desks, what not to re-ask. **Skipping it is how this desk asks the
owner the same question twice** — and when he is away, a re-ask is noise he
cannot even dismiss. Write what must survive your own run back into it.

That file is **gitignored** (`.gitignore:52`, `memory/`): it travels in the
backup zip and never reaches GitHub. **Personal facts belong there and nowhere
else in this folder** — `README.md`, this file, and `.claude\` are all public.

## The notes are the product

Capturing and answering questions about the user's personal notes, strictly per
`README.md`. **Check whether the server is up first** (`http://localhost:5111`):
while it is up you must go through the API, because a direct file edit bypasses
its content-hash conflict check and can clobber a line. Only when it is down is
an append-only file edit correct.

**Not everything in `#daybook` is a note.** A message asking *about* the notes
is a question — answer it, do not store it. Store what is meant to be recorded.
When it is genuinely ambiguous, ask rather than silently writing to the user's
personal notes.

Personal data stays here: never copy it into project folders or project memory.
