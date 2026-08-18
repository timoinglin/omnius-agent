# tools\whisper — audio → text (built)

Contract (stable regardless of engine):

- `python tools\whisper\transcribe.py <audio-file>` → prints the transcript (UTF-8, stdout); non-zero exit + stderr on failure. Language auto-detected.
- Accepts anything ffmpeg can read: `.ogg` (Discord voice notes), `.mp3`, `.wav`, `.m4a`, …
- Model size via `WHISPER_MODEL` env or `config\transcribe.ini` `[whisper] model` — default `base` — fast, and usually enough. Measured 2026-08-18 on a real four-second note: `base` heard *"Proderas, cuento 7, mas 7"* where `small` heard *"Prueba, cuanto es 7 mas 7?"*, correct for ~3x the time; this instance's owner chose speed knowingly. **Set `model = small` if a mishearing costs more than ten seconds** — a desk acts on what it heard.
- Spoken language via `[whisper] language` (ISO code, e.g. `es`); empty auto-detects. A belt, not the fix — it removes a guess for free.
- `python tools\whisper\prewarm.py` downloads the model up front — run by `install.bat`, idempotent.

Used by: sessions handling Discord voice notes/audio (ARCHITECTURE §3.4 media pipeline), `#daybook` voice capture.

Engine default (installed by root `install.bat`): **local faster-whisper** — offline, free, no key needed. **Note:** the model cache (`~/.cache/huggingface`, ~140MB) is machine-local and does **not** travel in the zip — each new machine downloads once (pre-warmed at install). Optional upgrade: API (Groq/OpenAI — faster on weak machines, key in root `.env`). The contract above doesn't change either way.
