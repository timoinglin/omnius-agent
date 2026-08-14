# tools\remotion — video rendering (placeholder — not built yet)

Shared capability for rendering videos programmatically with [Remotion](https://github.com/remotion-dev/remotion) (Node/React-based). Root `install.bat` installs `remotion` + `@remotion/cli` into this folder; without Node.js it is skipped.

**`node_modules` stays out of git *and* out of the zip** — `install.bat` recreates it on every machine (needs Node + network, ~240 MB). Verified against a real `pack.ps1` archive, 2026-07-25.

**Not yet usable:** there is no entry point — no `src/index.ts`, no `remotion.config.ts`, no render script — so nothing here renders anything today. A usage README and the render contract land with the first real use. Any session may use it; only tool sessions and the orchestrator maintain it.
