# LESSONS — why the rules in `/omnius` exist

Every rule in `.claude\skills\omnius\SKILL.md` was paid for. The skill keeps the
rule; the story lives here, so the file that loads on every headless run stays
short. One bullet per incident, dated.

## Identity & the run model

- **2026-08-01** — a headless run resumed a transcript shared with a terminal
  session, inherited the terminal's "a run is handling it, I stand down"
  reasoning, and stood down FROM ITSELF. The message went unanswered while its
  own worker waited for itself. → Believe the check-in's identity line over
  anything the conversation suggests.
- **2026-08-01** — session-side inbox watchers and claim heartbeats were
  deleted. Turn-based sessions cannot host daemons: the watcher died at every
  turn boundary, the desk went deaf, and the watchdog spawned duplicate brains
  onto occupied desks. → Reachability is the watchdog's job, never the session's.
- **2026-08-01** — orchestrator runs were resuming an 11 MB dev transcript to
  answer 52 KB of chat. → `resume: "fresh"`, plus the owner's rule: "the
  orchestrator has to be fast, and read memory only when needed".
- **2026-08-01** — owner decision: a single-component project's `#general`
  routes straight to its desk; only multi-component projects make the
  orchestrator resolve `category`.

## Answering

- **2026-07-31** — four re-sent voice notes in three minutes, ending in *"I sent
  the first audio almost 5 minutes ago and still have had no response of any
  kind."* Discord shows nothing between his message and the reply, so ten quiet
  seconds and ten quiet minutes look identical from a phone. → Acknowledge first.
- **2026-07-31** — user decision: `channel: "daybook"` envelopes belong to the
  daybook desk, captured strictly per `daybook\README.md`.
- **2026-08-04** — a project desk was spawned with its mandate, received **no
  mail at all**, and posted twice anyway while he sat at that very terminal
  watching the same text arrive in its channel: *"Why do I get messages in
  Discord if I am in CLI?"* → No envelope, no post. The same desk, restarted
  later that day, went straight back to posting, which is why the rule also
  lives in the root `CLAUDE.md` — a hand-opened desk never runs the skill.
- **2026-08-04** — one `/omnius` at spawn turned every later keyboard turn into
  a Discord copy of what he was already reading. → The contract ends with the run.
- **2026-08-06** — `#transcribe` sent a markdown table; it arrived as a wall of
  literal pipes and he sent back a screenshot. → Discord renders no tables.
- **2026-08-13** — a project desk ended an hour of real work with *"Voy a
  construir X. Te aviso cuando esté."* Nothing was building and no notice was
  ever coming. Indistinguishable from a crash, and worse, because it reads like
  progress. → Never promise to continue; open a counted work loop instead.
- **2026-09-02** — *"better inform user every couple of minutes that session is
  working."* A desk past ten minutes with his mail queued was told to reply
  `!restart`, which he reads as *it is dead* — and restarting kills the honest
  work the message was describing. The 2026-08-12 half-fix (one "still working"
  line at minute 15) still left fourteen silent minutes to read as death, and
  the terminal-turn path said nothing at all while working. → One notice every
  `[bus] working_notice_minutes`, on both paths, stopping the moment the desk
  speaks for itself; `!restart` only for genuine silence.

## Prompts nobody can see

- **2026-08-02** — `Remove-Item` on an envelope raised a permission prompt on
  every single Discord message (no allow-list sanely covers a delete). He
  answered four in `#alerts` and gave up. → `--ack`, never a shell delete.
- **2026-08-02** — an ad-hoc `$ms = [DateTimeOffset]::UtcNow…; $obj |
  ConvertTo-Json` to write an outbox file matched no allow rule and froze
  `daybook` and a project desk for 40 minutes each; both had drained their mail
  and could not say so. → Write outbox files with the `Write` tool.
- **2026-08-02**, same day — a `python -c` payload that built the JSON inline
  had its backslashes and backticks eaten by Git Bash and posted a garbled reply
  to Discord.
- **2026-08-02** — two desks sat mid-turn for 54 minutes holding the owner's
  "ping XD", frozen on permission dialogs, while every surface reported them
  healthily busy.
- **2026-08-04** — a scaffold used `git -C <path> …`; the flag pushes the verb
  out of the allow-list's reach and cost him three `ok`s. → One git command per
  call, from the repo's own folder.
- **2026-08-04**, his first message from home — asked "all working?", the desk
  hand-rolled PowerShell over `state\sessions\`; the second ask timed out and
  the session sat on a local dialog for 2.5 hours with his mail unread. → Use
  the sanctioned verb; a prompt he cannot see is a freeze, not a question.
- **2026-08-06** — after watching a desk stall on a dialog in a window he was
  not looking at: *"Over discord make everything auto allow, no allow questions,
  no matter where … what you can do is that you as LLM ask twice."* Deny-lists
  were emptied to `.env`; the model became the brake. Full statement in
  `memory\shared\USER.md`.
- **2026-09-02** — `tools\discord` had no `.claude\settings.json` at all, so
  every `python` call on that desk was refused and the run could not even reach
  its own check-in. Twelve runs failed five minutes apart; the failure ledger
  counted all twelve and **nobody was told**, because the desk is reached only
  by desk mail, has no channel, and the alert resolved `None` and gave up. The
  audit meant to catch this only looked at folders that already had a
  settings.json, so the missing file was also the reason the folder was never
  inspected. → An alarm falls back to the mail's origin channel and then
  `#alerts`; and a `tools\` folder with no profile is a **library** — the
  watchdog refuses to run it and refuses desk mail addressed to it, rather than
  starting a run that cannot speak. The fix he rejected is worth recording too:
  giving the seven libraries a profile each would have quieted the audit by
  inventing seven desks nobody wants.
- **2026-09-03** — a new check asserted what a **live `memory\`** file says.
  `memory\` is gitignored biography, so it does not travel: the suite passed on
  the machine that wrote the check and went red on every other install, and
  `update.ps1` correctly rolled the release back. → Assert content against
  `templates\fresh\memory\`, never the live copy (reading a live file to measure
  its SIZE is fine — those checks skip an absent file). **The suite that matters
  is the one inside the zip**, so `release.ps1` now unpacks it and runs it there
  before publishing.
- **2026-08-06 / browser** — the Chrome extension refuses to act with more than
  one browser connected and demands a pick, which is a click in Chrome. His
  objection: *"how am i gonna accept if i am on mobile on discord?"* → The
  choice is a setting, read at the desk.

## Delegation, guests, routines

- **2026-08-12** — `config\guests.ini`: real people who are not him, let into
  one desk's channels on purpose. Usually the project's real-world owner — they
  give product/UX direction, technical decisions stay with the machine's owner.
- **2026-08-18** — `config\telegram.ini`: some guests have no Discord account.
  Same envelope shape, but they see only that one channel.
- **Desk mail** replaced writing envelopes straight into `state\inbox\`: the
  direct path skips every check and leaves no visible trace (docs\DELEGATION.md).
- **A wrong schedule fails silently, at a time he is not watching** — the worst
  failure shape in this system. Three echoed fire times catch a misparse now
  instead of after three weeks of nothing happening. An hourly "nothing new"
  nine times a day trains him to ignore the channel, and then the one that
  mattered is ignored too.
- **Loops** (docs\DELEGATION.md D5) replaced the two-continuation honor system:
  an uncounted self-addressed continuation is refused outright.
- **2026-09-03** — Claude's API answered 529 for 85 minutes. The watchdog logged
  "turn ended in an API error" 25 times and said nothing in Discord, so the one
  fact he could not have guessed — that none of it was his fleet's fault — was
  the only one no surface carried. Worse, it *acted*: a healthy bridge was
  killed and reopened six times, each kill leaving a dead "Press any key"
  window, and the deadman advised `!restart`, which he followed. → Say when the
  fault is Claude's, hold every remedy while it lasts, and keep a window open
  only for a genuine boot failure. **An alarm whose advice is wrong is worse
  than silence: it spends the trust the next real one needs.**
- **2026-09-03** — *"why did it ask for an ok for a delegation? this shouldn't
  happen."* A project desk mailing `orchestrator`, asking for a category to be
  created, sat behind the D4 cross-project gate waiting for `ok ff5ff9`. Two
  faults in one prompt: escalating to the orchestrator was never a boundary
  breach — it is the only route for a request only that desk can act on — and
  the gate itself contradicted his older, broader rule (2026-08-13: auto-allow
  everything, the model is the brake). → Mail **to** the orchestrator is always
  free, and `cross_project_requires_ok` defaults to **0**. The key survives for
  anyone who wants the boundary back. The fleet must never ask permission to
  talk to itself.
