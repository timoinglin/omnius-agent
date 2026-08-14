---
name: omnius
description: Connect this transcribe session to the Omnius bus (delegates to the workspace-root skill).
---

# /omnius — bus connect (transcribe stub)

A session only discovers skills from its own `.claude\`, so this stub delegates.

1. Resolve the **workspace root**: the nearest ancestor containing `tools\`,
   `projects\` and `memory\`. From here that is **two levels up**
   (`<root>\tools\transcribe`).
2. Read `<workspace-root>\.claude\skills\omnius\SKILL.md` — the single source of
   truth — and follow it exactly.
3. Your session id is **`tool.transcribe`**. Your channel is `#transcribe`.

## Before answering anything, read `README.md` in this folder

It defines the two jobs this desk does and — more importantly — **the one rule
that keeps it usable: never start a long job inside your own turn.**

## The two shapes of work you will get

**A new recording** — a path, or "the meeting from this morning". You do NOT
transcribe it yourself. You start the detached job and return immediately:

```
python run.py "<path>" --detach --channel <the envelope's channelId>
```

**Always pass `--channel`.** It is how the finished job knows where to answer —
he may have asked from `#omnius`, not here. Omit it and the completion lands in
`#transcribe` regardless, which is a worse guess than the one you already have.

That prints JSON and exits in about a second. Tell him it started, roughly how
long it will take (~1.4× realtime ÷ workers, so a 2 h recording ≈ 25 min), and
where it will land. Then **end your turn** — you are free for the next question
while python grinds.

**An envelope `from: "transcribe-job"`** — the job finished (or failed) and is
handing the thinking back to you. It contains the instructions. That is when you
read the transcript, aim the dense frames, and write `notes.md`.

**Then POST.** A completion always goes to Discord — answer the envelope's
`channelId` if it has one, otherwise `#transcribe`. This is not the narration
the quiet rule forbids: he asked minutes or hours ago and walked away, so the
result is the one thing he cannot already see. A silent completion means the
feature does not work from his phone. (This exact mistake happened on the first
smoke test, 2026-08-06 — see README.)

## Never paste `INDEX.md` into Discord

It is a markdown table because it is a **file**. Discord renders no tables, so
pasting it through sends him a wall of literal `|` pipes — which is exactly what
happened 2026-08-06 answering "what have you transcribed". Read it, then write
bullets:

```
📼 1 recording

**2026-08-06** — Meeting with the CEO, 1:54
tooling tour, the AI mandate
`media\recordings\2026\2026-08-06-ceo\`
```

Same for any listing: files, frames, chunks. Bullets, or a fenced code block
when columns genuinely matter.

## Hard limits

- **Never run `transcribe_long.py` in your own turn.** 25 minutes of blocked
  desk is the exact complaint this desk was built to answer (2026-08-06).
- **Never write the summary into `daybook\notes\`** — that folder is pushed to
  GitHub and these recordings are internal company material. A pointer line
  only. See README.
- **Never write into `state\`** except your own `state\outbox\tool.transcribe\`.
- Your memory holds **how to do the job and an index of what you processed** —
  never what any recording said. That lives in its own `notes.md`.
