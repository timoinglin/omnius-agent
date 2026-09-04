# Permission profiles — ACTIVE

> **Status: the curated allow-list below was REVERSED on 2026-08-06, and widened again on
> 2026-08-19 — read this first.**
> What ships today, in every desk's `.claude\settings.json` (stamped by
> `tools\discord\sync_permissions.py`, which is the only writer), is the opposite shape:
> **everything is allowed except `.env`.** Concretely: 60+ tools including bare `Bash` and
> `PowerShell`, `defaultMode: acceptEdits` so file edits never prompt,
> `enableAllProjectMcpServers` so a project's MCP servers are trusted rather than asked
> about, a standing authorisation in `autoMode`, and every installed skill reachable as a
> slash command with no `config\skills.ini` needed.
>
> **And the MODE is named, not inherited — added 2026-09-04, because that file only decides
> anything if the session runs in a mode that consults it.** `config\fleet.json` now sets
> `permissionMode: "acceptEdits"` on the defaults and every role, and all three launch paths
> pass `--permission-mode` (`watchdog.start_run`, `desk_bridge.claude_argv`,
> `fleet_ops.open_desk`). It used to be `null` — *pass no flag* — which was fine until Claude
> Code 2.1.261 made **`auto`** the default: the auto-mode classifier judges each command on
> its own and **ignores the allow-list entirely**, then blocks outright after five in a row.
> Two gates ran at once — the `PermissionRequest` hook answered *allow* from Discord and the
> classifier asked again on a dialog no Discord reply can reach — and the owner spent four
> days answering `ok` to a fleet whose settings files were already perfect. A desk is only as
> permissive as the mode it starts in; `desk_audit.py` now asserts both halves.
>
> The deny list is `.env` (read, edit and write, at every depth a desk can sit at) plus
> `AskUserQuestion`. None of the fine-grained denies written below (`git push`, `rm -rf`,
> `npm publish`, `curl`, `iwr`, `taskkill`) exist in any shipped file.
>
> **Why `.env` stays denied while everything else opens (2026-08-19):** it is the one file
> that is not the owner's work but the owner's credentials, and desks now answer other
> people too — guests and Telegram invitees. It is a guardrail, not containment: `Bash` is
> allowed, so a desk that means to read it can. What contains this machine is who may talk
> to a desk at all, not which tool a desk may pick up.
>
> **Why `AskUserQuestion` stays denied:** not danger — answerability. It draws a menu in a
> terminal nobody is sitting at and blocks the very turn that would have to end for the
> owner to reach it (proven 2026-08-13). Denied, the desk asks in plain text, which he can
> answer from a phone.
>
> **Why it was reversed:** the narrow list did not fail safe, it failed *silent*. Every
> unlisted command became a dialog on a screen nobody was watching, and desks froze mid-task
> — 40 minutes each on 2026-08-02, 2.5 hours on 2026-08-04. His instruction: *"Over discord
> make everything auto allow, no allow questions, no matter where … what you can do is that
> you as LLM ask twice."* So the brake moved from the config into the model: routine work
> never asks, and anything irreversible or wide-blast is asked about **in words** first
> (the list is in `memory\shared\USER.md` and the `/omnius` skill). `AskUserQuestion` is
> denied because it draws a menu in a terminal nobody is sitting at.
>
> `python tools\discord\desk_audit.py` verifies that posture on every desk.
> The staged profiles in `docs\profiles\*.json` are a **historical variant** of the 2026-07-24
> proposal and no longer match what ships.
>
> Everything below is kept because the *reasoning* still matters — especially "unlisted ≠
> denied" and the escalation design, both of which survived the reversal intact. Read it as
> the record of how the dial got where it is, not as configuration to copy.

## Why this exists

A session spawned by the watchdog from a Discord message has **nobody at the keyboard** to answer a Claude Code permission prompt. The parity doctrine (ARCHITECTURE §2.4, §7) makes it a hard rule: *a remotely driven session must never hit a harness prompt.* So routine work must be pre-approved in committed settings; everything dangerous stays gated and is asked **in conversation** (which relays to Discord), never via a silent harness prompt.

During the 2026-07-23 shakedown the demo sessions ran with `--permission-mode bypassPermissions` (safe there: throwaway sandbox project). That is the crude stand-in this proposal replaces with a curated allow-list.

## The dial, in three positions

1. **Ask (default today)** — every mutating action prompts. Safe locally, **stalls remotely**.
2. **Scoped autonomy (proposed)** — pre-approve safe, in-scope work; keep destructive/global actions gated. This is the recommended remote setting.
3. **bypassPermissions** — no checks. Only for throwaway sandboxes, never a real project or the orchestrator.

## Proposed 2026-07-24, superseded 2026-08-06 — project session

*(Historical. What ships is bare `Bash`/`PowerShell` with only `.env` and `AskUserQuestion` denied — see the banner.)*

```json
{
  "permissions": {
    "allow": [
      "Read", "Edit", "Write", "Glob", "Grep", "LS", "NotebookEdit",
      "Bash(git status:*)", "Bash(git add:*)", "Bash(git commit:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git restore:*)",
      "Bash(python:*)", "Bash(node:*)", "Bash(npm run:*)", "Bash(npm test:*)", "Bash(pytest:*)",
      "Bash(ls:*)", "Bash(cat:*)", "Bash(mkdir:*)", "Bash(echo:*)"
    ],
    "deny": [
      "Read(./.env)", "Read(./**/.env)",
      "Bash(git push:*)", "Bash(rm -rf:*)", "Bash(npm publish:*)", "Bash(curl:*)", "Bash(iwr:*)"
    ]
  }
}
```

Rationale: a component session can freely read/edit/write **inside its own folder** (cwd scope is already enforced by role, root CLAUDE.md §4), run its own tooling and tests, and commit to its own repo — but cannot push, delete recklessly, publish, or exfiltrate over the network without asking. Tune the stack-specific lines per project.

## Proposed 2026-07-24, superseded 2026-08-06 — orchestrator

The orchestrator additionally manages the fleet. Suggested extra allows: the Discord helper CLI and the launchers — e.g. `Bash(python tools\discord\api.py:*)`, `Bash(python tools\discord\inbox_watch.py:*)`. Keep denied: `git push`, project **deletion**, `taskkill` of anything but via the watchdog's own `!kill`, and anything touching another machine. Fleet-destructive verbs (`/archive-project`, delete) stay confirmation-gated regardless (ARCHITECTURE §7).

## How to turn it on

1. Read the blocks above and adjust to your comfort.
2. Paste into `templates\project\.claude\settings.json` (projects) and root `.claude\settings.json` (orchestrator).
3. Commit — they travel in the zip, so every instance inherits the same profile.
4. Re-run the shakedown's `/omnius` (bus-connect) flow **without** `bypassPermissions` and confirm no prompt appears for routine work; anything that does prompt is either a real gap in the allow-list or correctly gated.

## Decision record

**2026-07-25 — remote permission escalation built (user-approved).** The hard rule "a remotely driven session must never hit a permission prompt" was never enforceable: one unforeseen tool call stalls the session on a dialog nobody can see. The workaround was to widen the allow-list until prompts stopped — which is how `Bash(python:*)` ended up making every `deny` entry reachable anyway. Escalation replaces widening.

- **Mechanism:** a `PermissionRequest` hook (`tools\discord\permission_relay.py`, registered in root and template `settings.json`). Claude Code runs it when a dialog is about to appear and applies the decision it prints; printing nothing falls through to the normal dialog. Hooks may block — the default hook timeout is 600s, ours is set to 180.
- **Flow:** session → `state\outbox\<id>\` (sessions still never touch Discord) → watchdog posts to `#alerts` → owner replies `ok` / `no` (with a 6-char code when several are pending) → watchdog writes `state\permissions\<id>.answer` → the hook returns `allow`/`deny`.
- **Fails safe, never open.** Silence is never consent: on timeout the hook prints nothing and the local dialog appears exactly as it does today. Only an explicit reply can allow.
- **Zero cost when local.** Only sessions with a live claim carrying a `discordChannel` escalate. A plain desktop session exits the hook immediately and sees no added latency.
- **Redacted:** the relayed command goes through `api.redact()` — a command line can contain a token and `#alerts` is a chat channel.
- **What this unlocks:** the profile can now be *tightened* — dropping `Bash(python:*)`/`Bash(node:*)` to named entrypoints and scoping the bare `Read`/`Edit`/`Write` grants — because the cost of a too-narrow list is a phone notification rather than a silently stalled session. **That tightening is not yet applied; it is the next decision.**
- **Not yet verified end-to-end through a real spawn:** the relay, the watchdog answer path and the id derivation are covered by the suite (120 checks), and the hook was pipe-tested with a real payload, but the project-template hook path (`${CLAUDE_PROJECT_DIR}/../../…`) is confirmed only by the settings layout, not by an observed spawn.

**2026-07-24 — user chose (a):** same scoped-autonomy list for the orchestrator, destructive gated. Orchestrator deviations from the project profile: `curl http://localhost:*` allowed (daybook/service probes are routine orchestrator work), `taskkill` denied (session kills go through the watchdog's `!kill` mechanism, never ad-hoc), network-fetch denies (`curl`/`iwr` general) not applied at root since localhost is needed — the gate against exfiltration remains the no-secrets rules + review. Fleet-destructive verbs stay confirmation-gated by the constitution regardless of tool permissions (CLAUDE.md §3, ARCHITECTURE §7).

---

## 2026-07-27 — tightened profile for the work instance (staged, not yet applied)

The user starts at a new job on 2026-07-28; a second instance runs on **employer
hardware against a company Discord server**. They chose *tighten for work*. The
tightening the escalation hook unlocked is now written down:

**Staged, ready to copy:**

| File | Applies to |
|---|---|
| `docs\profiles\orchestrator.settings.json` | root `.claude\settings.json` |
| `docs\profiles\project.settings.json` | `templates\project\.claude\settings.json` + each `projects\<name>\<component>\.claude\settings.json` |

**What changed and why**

- **Scoped file grants.** Bare `Read`/`Edit`/`Write` (any path on the machine)
  become `Read(./**)` / `Edit(./**)` / `Write(./**)`. For a project session
  `./**` is its own desk and `Read(../../**)` is the workspace — encoding root
  CLAUDE.md §4 (*read the whole workspace, write only your component*) as an
  actual rule instead of a convention.
- **Named entrypoints replace `Bash(python:*)` / `Bash(node:*)`.** Those two made
  every `deny` reachable — `python -c` reads `.env` regardless of `Read(./.env)`.
- **Paths stay relative.** These files travel in the zip; an absolute path would
  break on every new machine.

**The design decision worth remembering: unlisted ≠ denied.**

A first attempt put `python -c`, `powershell` and `cat` in `deny`. That is wrong.
`deny` is absolute and **cannot be escalated**, so denying broad shells re-creates
the silent stall this whole mechanism exists to prevent — and would have blocked
`pack.ps1` and `install.ps1` outright. Anything not routine is therefore simply
**left out** of `allow`: it falls through to the `PermissionRequest` hook, reaches
`#alerts`, and waits for `ok`/`no`. `deny` is reserved for what must never happen
(reading `.env`, `git push`, `rm -rf`, `npm publish`, `taskkill`; plus network
egress for project sessions).

**Applying it is a user action, deliberately.** An agent rewriting its own
permission file is gated by the harness classifier — correctly. Copy the staged
files over, or use `/update-config`.

**The staged files carry no `hooks` block, and neither does any tracked
settings.json (2026-08-14).** A hook command is an absolute path, so it is
machine state: `install.bat` writes the five hooks into every desk's
`.claude\settings.local.json` (gitignored, excluded from release zips) via
`tools\discord\fix_hook_paths.py`. Copying a staged profile over therefore
changes permissions only and leaves the hooks alone. Before this split, the
staged profiles still showed the `${CLAUDE_PROJECT_DIR}/../../` spelling that
was abandoned on 2026-08-02 — copying one would have re-introduced a hook path
that resolves differently for every session depth.

**Not verified against a live spawn.** Same caveat as the escalation hook itself:
the shapes are asserted by the suite, but no unattended spawn has run under this
profile. Expect to approve a few things in `#alerts` on day one; if something
routine keeps asking, add it to `allow` rather than widening a wildcard.

**Company-server note.** Relayed permission prompts include the command line
(token shapes redacted) and land in `#alerts`. Set that channel's visibility
accordingly.

**2026-08-15 — the cross-project desk-mail gate reuses this rail's grammar, not its hook.** Desk-to-desk delegation (`docs\DELEGATION.md`) holds an envelope that crosses a project boundary in `state\gate\` and asks ok/no in Discord with the same word list and 6-char codes as a permission ask. The differences are deliberate: it parks a **file**, never blocks a hook, and a blocked hook always outranks a parked envelope — while permission asks are pending, a bare `ok` answers the permission, and a gate then needs its code. Unanswered gates fail closed after 60 minutes; silence delivers nothing.
