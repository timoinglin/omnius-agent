# Naming — channels, and the agent itself

What a channel is called, and what this agent is called, are settings. Neither
is identity. Reader docs: `docs\DISCORD.md` par. 2, `docs\GUIDE.md` par. 1.

- **Routing is pinned by channel id.** The desk behind a channel is written to
  `state\watchdog\channels.json` the first time that channel is created or
  recognised (key = the desk id; session-less schema channels use
  `<category>#<name>`). `build_map()` asks the pin before any name rule and
  pins whatever it derives, so the name rules are only ever a first sighting.
  He may rename any channel in the Discord app and its desk still answers.
- A **deleted** channel drops its pin and the next stamp recreates it — a
  deletion is not a rename. Pins are machine-local and `state\` never travels
  in the backup zip, so a workspace restored on another PC matches by name for
  one round: a channel that had been renamed comes back **unmapped and says
  so**, and renaming it back re-pins it.
- **A relay is not a home.** A project's `#general` maps to a desk but must
  never take that desk's pin, or he is answered in a project channel forever.
- **The agent's name** is `config\omnius.ini` `[omnius] name`; install asks
  for it, default *Omnius*. It names the orchestrator's channel and the
  terminal tab. Only the ADDRESS moves — the folder, the repo and the
  `/omnius` skill keep theirs whatever he chose, which is why every check-in
  prints *"You are &lt;name&gt;"*: a desk cannot infer it.
- **Never put a `{placeholder}` in `schema.json`.** That file is read by
  whatever watchdog is currently in memory, so an instance that pulled a new
  copy but has not reloaded stamps the placeholder verbatim — eleven `#agent`
  channels on a live server, one a minute, 2026-08-24. The pattern is
  `"name": "<default>"` plus `"namedAfter": "agent"`, and `ensure_structure`
  refuses any name still holding a brace.
