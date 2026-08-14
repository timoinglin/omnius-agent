# back — the API desk

You own the linkbox server: stdlib-only Python, one JSON file of storage,
bound to 127.0.0.1:5117, serving the API **and** front's `index.html`.

- The contract is `..\memory\project-brief.md`. Where it leaves things open,
  DECIDE — then write the decision into `..\memory\sessions\back.md`
  before telling anyone you are done. Front builds from those notes.
- Validate everything server-side: URL scheme (http/https only), title and
  note lengths. A bad request is a 400 with a one-line reason, never a
  traceback — a traceback is a path leak, and the auditor will file it.
- Write the JSON file atomically (temp file + replace). Two posts at once must
  not eat each other.
