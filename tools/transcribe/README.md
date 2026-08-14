# transcribe — the recordings desk

Session id **`tool.transcribe`** · desk `tools\transcribe\` · channel **`#transcribe`**

Built 2026-08-06 for a 115-minute CEO call. The goal is not a transcript; it is
being able to ask *"what tools did he show me for HR?"* three weeks later and
get a real answer. Meetings, video calls, ordinary videos, plain audio — same
pipeline, no distinction.

| Command | Does |
|---|---|
| `python run.py <src> --detach` | **the desk's entry point** — starts the job, returns in ~1 s |
| `python run.py <src> --detach --no-frames` | same, transcript only — when he asks for words and no screenshots |
| `python transcribe_long.py <src> --out <dir>` | timestamped transcript, chunked across CPU workers |
| `python frames.py <src> --out <dir>` | the frames where the SCREEN changed — shared windows, dashboards, demos |

## The one rule: never grind inside your own turn

A two-hour recording takes ~25 minutes to transcribe. A desk that spends those
25 minutes inside a turn answers nothing, which is the exact complaint that
produced this desk — *"i cannot ask or use omnius anymore until that job is
finished."*

But the grinding needs no model. So `run.py --detach` spawns a plain python
process and returns at once:

```
you ──> #transcribe ──> run.py --detach ──> (desk free, answers you again)
                             │
                         ~25 min of ffmpeg + whisper, zero tokens
                             │
                             └──> state\inbox\tool.transcribe\*.json
                                        │
                          watchdog scan ─┴──> a FRESH run reads the transcript,
                                              aims dense frames, writes notes.md
```

Nothing here is new machinery: the scheduler in `watchdog.py` already delivers
non-Discord work by writing that same envelope shape, and `ensure_runners()`
scans the inbox, so dropping the file *is* the handoff.

**Two runs, both short.** The desk never blocks, and no envelope waits behind a
job.

### A finished job ALWAYS posts

The quiet rule (root CLAUDE.md §5) says don't narrate — he can see his own
screen. **A completion is the one thing he cannot see.** He sent a recording and
walked away; the job lands minutes or hours later, and if the desk stays silent
the feature does not exist for the phone, which is the whole point.

This is not theoretical. The first smoke test, 2026-08-06, ran the loop
perfectly and then said nothing — the desk's own reasoning: *"no outbox post
(the envelope carried no `channelId` — nothing on Discord asked)."* Sound
instinct, wrong case. So the ambiguity was removed rather than argued with:
`run.py --channel <id>` stamps the channel he asked from onto the completion
envelope, and the brief says *post* in words.

**Answer the envelope's `channelId` when it has one** — that is where he asked,
and it may be `#omnius` rather than here. Otherwise post to `#transcribe`.

### Why `Write`/`Edit` are unscoped in `.claude\settings.json`

The same smoke test found the second half of that bug. This desk's settings were
copied from `tool.fleet`, whose `Write(../../state/outbox/**)` is right for a
**read-only** desk — and the desk reported that pattern matching *nothing*: its
reply was refused twice and it fell back to `api.py` to get the message out.

`daybook` and the root profile allow-list bare **`Write`** and **`Edit`**, which
is what `/omnius` means by *"`Write` is allow-listed on every desk … so it never
prompts."* A scoped rule that silently fails is the 40-minute invisible freeze
that skill warns about, and **a desk that cannot say it finished is worse than
one that can write widely.** The `deny` list is the real fence: no `.env`, no
`git push`/`commit`, no `rm -rf`, no `taskkill`.

> ⚠ **`tool.fleet` probably has the same latent problem** — same pattern, never
> exercised. Worth checking before it needs to answer something urgent.

### A silent recording is a failure, not a fast job

2026-08-14: a 22-minute OBS capture finished in **0.6 min** and announced DONE.
Whisper's VAD had heard nothing at all — the file carried an audio track, but at
a flat **−91 dB** (2 kb/s AAC): OBS was recording with no audio input routed, so
the meeting was never captured. Nothing about the run said so; `transcript.txt`
was simply 0 bytes.

`run.py` now checks the transcript before announcing and reports a **failed**
job with the diagnosis, so no run is ever sent to read an empty file. To confirm
by hand:

```
ffmpeg -i <src> -vn -af volumedetect -f null NUL
```

`max_volume: -91.0 dB` means digital silence — the recording cannot be rescued,
only re-made. Suspiciously fast is the tell: transcription runs at roughly
1.4× realtime ÷ workers, so anything finishing in seconds heard nothing.

## What this desk must NOT do

- **Never run `transcribe_long.py` inside a turn.** Always `run.py --detach`.
- **Never put a recording's content in `daybook\notes\`.** That folder is
  tracked and pushed to GitHub; these recordings hold named employees, HR cases
  and salary references. A **pointer line** is the whole permitted footprint:
  `- 11:03 Meeting Nacho (CEO), 115 min → media\recordings\2026\…\`
  Append it through the daybook **API** (`python`, not `Invoke-WebRequest` —
  that prompts into `#alerts` on a headless desk). If the server is down, say
  so rather than writing the file directly; the daybook owns its own storage.
- **Never let a recording's content into this desk's memory.** Its memory holds
  *how to do the job* and an *index of what was processed*. After twenty videos
  a fat desk memory would recreate the context problem one level down. Each
  recording's knowledge lives in its own `notes.md`.
- Never write into `state\` except `state\outbox\tool.transcribe\`.

## Answering questions later

**The desk produces; anybody reads.** `media\` is transparent to every session
(root CLAUDE.md §2), so the orchestrator answers *"what did he say about MCP"*
straight off `notes.md` in whatever channel he is already in. He should not have
to come here to ask a question.

Send it here when the answer needs the **raw transcript loaded** or a **new
frame pulled** at a timestamp nobody extracted yet — the things worth spending
a whole context on.

## Why it is built this way

**Long recordings are a scheduling problem, not a transcription problem.**
Measured on this laptop: `small` runs at 1.4× realtime — 82 minutes for a
115-minute call — and `base` runs at 8.8× but mangles product names, which is
precisely what gets asked about later. So the model stays `small` and the time
is bought back with cores: chunks transcribed in parallel, merged onto one
timeline.

Chunks **overlap**, because a hard cut lands mid-word and both sides lose it.
On merge, segments starting inside the previous chunk's overlap are dropped so
the seam neither stutters nor repeats a sentence.

The decoder is seeded with domain vocabulary (`--prompt`). It measurably
improves proper nouns, which is the only reason the slower model was worth it.

**Scaling to a bigger machine.** The limit on the 2026-08 laptop was RAM, not
cores: 3.8 GB free capped it at 3 workers, each left with 2 threads. On a
machine with more of both, raise `--workers` and then consider spending the
headroom on `--model medium` rather than on finishing sooner — product and
tool names are what get asked about weeks later, and that is exactly where the
bigger model earns its keep. Both are already flags; nothing needs rebuilding.

Do not expect help from an integrated GPU or an NPU: faster-whisper runs on
CTranslate2, whose accelerated path is CUDA. On AMD integrated graphics it
stays on the CPU.

**Frames are scene changes, not a sample every N seconds.** Two hours of talking
heads is worth no screenshots at all; the moment a screen is shared is worth
several. A burst of changes inside a few seconds is one event, and when there
are more than the cap they are kept **evenly spread** — otherwise a busy first
ten minutes eats the budget and the last hour is invisible.

Frame filenames are the timestamp (`t03720.jpg`), so any image can always be
tied back to what was being said at that moment.

**Then go back for the parts that matter — `--from` / `--to`.** Measured on the
first real call: one wide pass at `--threshold 0.30` found the meeting's shape
but *missed the HR portal entirely*. The reason is worth remembering, because it
is the normal case rather than an exception: he shared his **whole desktop**,
which arrives as a small window inside the recording, so navigating from one
Zoho page to another moves the whole-frame scene score almost not at all.

The fix is not a lower threshold everywhere — at 0.06 across two hours you get
hundreds of near-identical talking heads. It is a **second, denser pass bounded
to the minutes that matter**, once the transcript has told you where they are:

```
python tools\transcribe\frames.py <video> --out <dir> --from 660 --to 1250 \
       --threshold 0.06 --min-gap 8 --width 1600
```

`frames.json` is **merged**, not overwritten, so passes accumulate. Read the
transcript first, then aim. And expect some frames to land mid-load — a web app
mid-render is a scene change like any other; take the next one.

## Where the output goes, and why not in git

`media\recordings\<year>\<date>-<slug>\` — because **`media\` is gitignored**.

That is deliberate and load-bearing: `daybook\notes\` is *tracked and pushed to
GitHub*, so a recording of a private company meeting must never be written
there. `media\` stays on the machine and travels only in his own backup zip.

Contents:

```
transcript.json    segments with start/end seconds — the machine-readable source
transcript.txt     [h:mm:ss] lines — what a human or an agent reads
frames.json        index: seconds -> frame file
frames\t*.jpg      the screens that were shared
notes.md           the distilled record: topics, decisions, actions, tools
```

## Answering questions later

`notes.md` first (it is short and structured), `transcript.txt` when a detail is
needed, and the frames when the question is visual — *"what did he show"*,
*"what was that dashboard"*. The timestamp on a frame maps straight into the
transcript, so a picture always comes with what was said over it.
