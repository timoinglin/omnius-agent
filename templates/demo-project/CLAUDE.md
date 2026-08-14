# {{PROJECT_NAME}} — linkbox (the shipped demo project)

A tiny link-sharing board, built by **three desks working as a team**. It exists
to show the multi-agent workflow on something real: two desks build against a
shared brief, a third audits what they built. Nothing here is pre-written — the
desks write the code when you ask them to.

## The desks

| Desk | Channel | Owns | May write |
|---|---|---|---|
| `back` | `#back` | the HTTP API + storage | `back\` and `memory\sessions\back.md` |
| `front` | `#front` | the web page | `front\` and `memory\sessions\front.md` |
| `auditor` | `#auditor` | finding problems | **ONLY** `memory\audits\` and `memory\sessions\auditor.md` |

## Project rules

- **The brief is the contract**: `memory\project-brief.md`. Build what it says;
  when you decide something it leaves open, record the decision in your session
  notes so your siblings build against it instead of guessing.
- **`back` publishes its API in `memory\sessions\back.md` BEFORE `front` builds
  against it.** Front reads those notes; front never reads back's code to guess.
- **The auditor is read-only.** It may read every file in this project, and it
  writes findings — never fixes. A finding names the file and line, says what an
  attacker or a leak could do, and rates it (HIGH / MEDIUM / LOW). Fixing is the
  owning desk's job, in its own channel, so the audit trail shows who knew what.
- Python is **stdlib only** (this whole workspace runs without a package for a
  reason); the front end is one HTML file, no build step, no framework.
- Never touch `..\..\.env`, and never put a secret, a token or a real email in
  this project — the auditor is told to hunt exactly that.

## The intended play (what the human does)

1. In `#back`: *"build the API from the brief"*
2. In `#front`: *"build the page against back's session notes"*
3. In `#auditor`: *"audit the project"* — then watch it find what the builders
   missed, and send each finding back to its owner's channel to fix.
