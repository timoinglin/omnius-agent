# Skills — the system's verbs

Skills land here as `<name>\SKILL.md` folders and travel with the repo (auto-discovered by Claude Code).

See docs\ARCHITECTURE.md §3.6:

| Skill | Runs in | Phase |
|---|---|---|
| `omnius` | any session | 2 ✅ |
| `new-project` | orchestrator | 3 ✅ |
| `spawn-session` | orchestrator | 3 ✅ |
| `status` | orchestrator | 3 ✅ |
| `archive-project` | orchestrator | 3 ✅ |

The **Discord watchdog is not a skill** — it's a standalone always-on script in `tools\discord\` (ARCHITECTURE §3.4). Skills are the verbs sessions run; the watchdog is the transport.

Status: **all five are built.** `omnius` (Phase 2; renamed from `remote-control` 2026-07-24 — that name collides with a Claude Code built-in command) and the four orchestrator verbs (2026-08-01).

The orchestrator verbs are **thin on purpose**: each skill owns the judgement (what to ask, what to confirm, what to write to memory) and calls `tools\orchestrator\fleet_ops.py` for the mechanics that must come out identical every time. All are **idempotent** — safe to re-run after a half-finished attempt, which matters because they are driven from a phone where a dropped reply looks exactly like a failure.
