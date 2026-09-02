---
name: tts
description: Turn text into a spoken audio file with the local Piper TTS engine, and send it as a Discord attachment. Run when the user asks for audio, a voice note, a spoken explanation, or "mándamelo en audio".
---

# /tts — text to speech, locally, with Piper

He asks for audio when reading is the friction (`memory\shared\USER.md`: he has
reading difficulties). A spoken answer is not a nicety for him, it is the
accessible version — so treat "mándamelo en audio" as a first-class request,
not an extra.

## First: find Piper, do not assume it

**The install path is config, not a constant.** Resolve `<PIPER>` before any
command — `[tts] piper_root` in `config\ai.ini`, default `C:\ai\piper`:

```
python -c "import sys;sys.path.insert(0,'tools');import omnius_config as c;print((c.load('ai').get('tts') or {}).get('piper_root') or r'C:\ai\piper')"
```

Then check that `<PIPER>\venv\Scripts\python.exe` actually exists.

**If it does not, stop and say so in one line** — *"no Piper on this machine;
set `[tts] piper_root` in `config\ai.ini` if it lives elsewhere, or I can answer
in text"* — and answer in text. **Do not install it, do not download voices, do
not fall back to a cloud TTS service.** A missing optional capability disables
exactly one thing and breaks nothing else (`config\ai.ini`'s own rule).

Where Piper *is* installed, his instruction, 2026-09-01, verbatim: *"It is
already fully set up — do not reinstall, do not upgrade, and do not start any
server."*

- ⛔ Never `pip install -U piper-tts`. Never `python -m piper.http_server`.
- ⛔ Never install anything outside `<PIPER>` without asking him first.
- CLI and the Python API only.

## The command that works

Write the text to a **UTF-8 `.txt` first** and pass it with `-i`. Console pipes
work for one short line, but the file route removes every codepage risk with
Spanish accents — and your text will have accents.

```
<PIPER>\venv\Scripts\python.exe -m piper -m es_ES-davefx-medium ^
    --data-dir <PIPER>\voices -i <utf8-text-file> -f <output.wav>
```

- **Spanish (default): `es_ES-davefx-medium`** — Castilian, single speaker.
- **English: `en_US-lessac-high`.**
- Also on disk: `es_ES-sharvard-medium`, `es_MX-claude-high`. **Do not offer the
  Mexican voice as an upgrade** — his content is Spanish public procurement and
  the accent is wrong. `es_ES` has no `high` tier; medium is the ceiling.
- Single-speaker models: omit `-s` / `speaker_id`.
- `--length-scale` **above 1.0 is SLOWER**, not faster. Also `--noise-scale`,
  `--noise-w-scale`, `--sentence-silence`, `--volume`.

Python API, tuning and streaming: `<PIPER>\README.md` and `<PIPER>\example.py`,
on disk beside the install.

**Speed, measured 2026-09-01:** 11.2 s of Spanish audio in 1.7 s wall clock,
model load included. A three-minute explanation is seconds of work — never a
reason to defer the request to a later run.

## Then: WAV is too big for Discord — convert

Piper writes 22.05 kHz mono WAV. Three minutes is ~8 MB, which is at Discord's
limit. `ffmpeg` is on PATH:

```
ffmpeg -y -loglevel error -i <in.wav> -ac 1 -ar 24000 -b:a 48k <out.mp3>
```

That took the same 3-minute file from 8.3 MB to 1.1 MB with no audible loss for
speech. **Archive the mp3 in `media\sent\YYYY-MM\`** with a dated, descriptive
name, delete the intermediate WAV, and attach the mp3 by absolute path in the
outbox envelope's `files` array.

## Write for the ear, not the eye

The text you synthesize is **not** the text you would post. Rewrite it:

- No markdown, no bullets, no headers, no code fences — Piper reads the
  asterisks and backticks out loud.
- **Do NOT spell numbers out.** Piper normalizes them correctly: write `9.454`
  and `164`, and it says them properly in Spanish. Spelling them as words is the
  common way to make the audio worse.
- Long identifiers — CPV codes, hashes, URLs, file paths — are unbearable
  spoken. Say *"los ocho códigos que están bien"*, and leave the list to the
  written message that accompanies the audio.
- Full sentences, one idea each. What reads as a terse bullet list becomes
  choppy and hard to follow as speech.

## The reply itself

Send the audio **with** a short written message — not instead of one. The text
carries anything he may need to copy, click or re-read (paths, codes, decisions);
the audio carries the explanation. Say how long it is, so he knows whether he
has time for it now.
