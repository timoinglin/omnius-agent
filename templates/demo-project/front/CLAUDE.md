# front — the page desk

You own `index.html` — one file, no framework, no build step, served by back.

- Build against `..\memory\sessions\back.md`, NOT against back's code. If
  the notes are missing something you need, ask in your channel; guessing an
  API shape is how siblings drift.
- **Titles and notes are hostile input.** Render them as text, never as HTML.
  A link's href needs care too — the brief allows only http/https, but the
  page should not trust that the server enforced it.
- Record your own decisions (escaping, empty states, "time ago") in
  `..\memory\sessions\front.md`.
