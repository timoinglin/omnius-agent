# tools\remotion — video rendering

Shared capability for rendering videos programmatically with
[Remotion](https://github.com/remotion-dev/remotion): compositions are React
components, frames are rendered in headless Chrome, ffmpeg encodes them.

Any session may **use** it. Only tool sessions and the orchestrator **maintain**
it. Built and first used 2026-08-22.

---

## Render contract

Everything below is the default the tooling already enforces — you only pass the
composition id and the output path.

- **1920x1080**, **H.264 mp4**, **CRF 16**
- **30 fps** — derive `durationInFrames` as `FPS * DURATION_SECONDS`, so a piece stays
  exactly as long as you meant it and switching to 60 fps changes nothing else
- Output goes to **`out\`**, which is **gitignored** — a render is a build
  artifact, not source
- Renders are **silent but not audio-less**: Remotion muxes a silent AAC track.
  That is why `ffprobe` reports `10.05` for a 10.000s video — AAC frame padding,
  not a timing bug. Pass `--muted` if you need the track gone.

Defaults live in `remotion.config.ts` (entry point, codec, CRF, overwrite), so
they never have to be repeated on the command line.

## Running it

From **this folder** (`W:\omnius\tools\remotion`):

```
npm run studio                 # interactive preview in a browser
npm run compositions           # list what is registered, with fps/size/duration
```

Rendering names the composition, so it is a CLI call rather than a script:

```
npx remotion render <CompositionId> out\<name>.mp4 --concurrency 4
npx remotion still  <CompositionId> out\<name>.png --frame 130
```

### Always pass `--concurrency`

**This machine is shared with other desks.** Remotion will otherwise take every
core it can see. `4` is the tested value; the full 300-frame render finishes in
a couple of minutes at that setting. Say in your reply what you used.

### The first render on a machine downloads Chrome Headless Shell

~113 MB from `storage.googleapis.com`, once per machine, before a single frame
renders. It is already downloaded on this one. On a fresh machine it needs
network — if it stalls, **say so** rather than sitting on it.

## Adding a composition

1. Write the component under `src\scene\`.
2. Register it in `src\Root.tsx` with a `<Composition>`.
3. `npm run compositions` to confirm it is picked up and the duration is right.
4. Iterate with `npx remotion still` — a still is seconds, a render is minutes.

Keep beats in **seconds**, not frames (see `src\scene\timing.ts`) and derive
`durationInFrames` from `fps`. Then switching 30 -> 60 fps in `Root.tsx` keeps
the piece the same length and the choreography does not drift.

## Layout

```
remotion.config.ts     render defaults (entry point, codec, CRF)
src\index.ts           registerRoot
src\Root.tsx           <Composition> registry - fps and duration live here
src\scene\timing.ts    the beat map, in seconds, + interpolate/decay helpers
src\scene\*.tsx        the pieces of a composition
out\                   renders (gitignored)
```

**Only this README and the pinned `package.json` are in git.** Everything
above it is yours: what you write here is your video, and a fresh install
should arrive with the tool, not with somebody else's trailer. The root
`.gitignore` enforces that, so a stray `git add -A` cannot publish your
compositions the way it did once (2026-08-24).

`node_modules` stays out of git **and** out of the zip — root `install.bat`
recreates it per machine (needs Node + network, ~240 MB). Verified against a
real `pack.ps1` archive, 2026-07-25. Never vendor it, never commit it.

## Gotchas found the hard way

- **The bundled ffmpeg does not start on this machine (2026-09-03).** A direct
  `npx remotion render` dies with `FFmpeg quit with code 3236495362 while piping
  frame 0` — that is `0xC0000142`, STATUS_DLL_INIT_FAILED, and `npx remotion
  ffmpeg -version` prints nothing at all. Stills are unaffected (they never
  touch ffmpeg). **Workaround: render the frames, then encode with the system
  ffmpeg** (`ffmpeg 9.0`, on PATH via winget):

  ```
  npx remotion render <Id> out\seq --sequence --image-format png --concurrency 4
  ffmpeg -y -framerate 30 -i "out\seq\element-%03d.png" -c:v libx264 -preset slow ^
         -crf 16 -pix_fmt yuv420p -movflags +faststart "out\<name>.mp4"
  ```

  This also gives a genuinely silent file — no padded AAC track, so `ffprobe`
  reports exactly `30.000000` rather than `30.05`.
- **Run Remotion from PowerShell, not the Bash tool.** Under the Bash sandbox the
  compositor cannot be spawned at all (`Error: spawn UNKNOWN`).

- **`margin` on a flex child that is being centred only moves it half as far.**
  `justify-content: center` centres the box *including* its margin. Use
  `transform: translateY(...)` for deliberate offsets — and put the translate
  *before* the scale, or the scale multiplies your offset.
- **Chromatic-aberration fringes go UNDER the fill, not over it.** Painted on
  top they wash the metal out; underneath, the fill covers their centres and
  only the offset edges show, which is what the real artifact looks like.
- **System fonts only.** `@remotion/google-fonts` would fetch at render time;
  the current piece uses Impact / Arial Narrow so it renders identically with no
  network. If you add a webfont, that becomes a new network dependency.
- **The bundled ffmpeg is a minimal build.** `npx remotion ffmpeg` /
  `npx remotion ffprobe` exist and are handy for verifying output, but filters
  like `tile` are missing — extract frames one at a time instead of building a
  contact sheet.
- **Verify what you shipped, not what you rendered.** `npx remotion ffprobe
  out\<file>.mp4` for duration/codec, then pull a frame back *out of the mp4*
  with `npx remotion ffmpeg -ss <t> -i ... -frames:v 1 out\check.png`.

