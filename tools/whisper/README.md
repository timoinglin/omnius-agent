# tools\whisper — audio → text (built)

Contract (stable regardless of engine):

- `python tools\whisper\transcribe.py <audio-file>` → prints the transcript (UTF-8, stdout); non-zero exit + stderr on failure. Language auto-detected.
- Accepts anything ffmpeg can read: `.ogg` (Discord voice notes), `.mp3`, `.wav`, `.m4a`, …
- Model size via `WHISPER_MODEL` env or `config\transcribe.ini` `[whisper] model` — default `base` — fast, and usually enough. Measured 2026-08-18 on a real four-second note: `base` heard *"Proderas, cuento 7, mas 7"* where `small` heard *"Prueba, cuanto es 7 mas 7?"*, correct for ~3x the time; this instance's owner chose speed knowingly. **Set `model = small` if a mishearing costs more than ten seconds** — a desk acts on what it heard.
- Spoken language via `[whisper] language` (ISO code, e.g. `es`); empty auto-detects. A belt, not the fix — it removes a guess for free.
- `python tools\whisper\prewarm.py` downloads the model up front — run by `install.bat`, idempotent.

Used by: sessions handling Discord voice notes/audio (ARCHITECTURE §3.4 media pipeline), `#daybook` voice capture.

## "not installed" almost never means not installed

The failing import is the one thing here that a machine can break in three ways, and two of them look identical from the outside. `load_whisper_model()` tells them apart instead of guessing:

- **A missing package** — `importlib.util.find_spec` comes back empty. The message names *this interpreter*, because the usual cause is `python` resolving somewhere `install.bat` never installed into (2026-08-12: install.bat had run into 3.11, miniconda then took the front of PATH).
- **PyAV blocked by an OS policy** — the package is right there and `av\_core.pyd` will not load. On 2026-08-31 **Windows Smart App Control** blocked it (*"Una directiva de Control de aplicaciones bloqueó este archivo"*) and every voice note came back as "faster-whisper is not installed" on a machine where it demonstrably was. faster-whisper imports `av` only in `audio.py` and only calls it inside `decode_audio()` — which `transcribe()` skips entirely when handed a numpy array. So `av` is stubbed and **ffmpeg decodes instead**, with a note on stderr. Nothing to install: this tool already documents ffmpeg as its input contract.
- **Neither** — the real `ImportError` is printed rather than a story about it.

`prewarm.py` and `tools\transcribe\transcribe_long.py` go through the same loader on purpose. Prewarming through a separate import is how a machine gets a green install and a red voice note.

If you would rather unblock PyAV than decode with ffmpeg, that is a Windows Security setting (*App & browser control*) and needs the owner at the PC — the fallback exists so nobody has to wait for that.

Engine default (installed by root `install.bat`): **local faster-whisper** — offline, free, no key needed. **Note:** the model cache (`~/.cache/huggingface`, ~140MB) is machine-local and does **not** travel in the zip — each new machine downloads once (pre-warmed at install). Optional upgrade: API (Groq/OpenAI — faster on weak machines, key in root `.env`). The contract above doesn't change either way.
