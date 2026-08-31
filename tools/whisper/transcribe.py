#!/usr/bin/env python3
"""tools\\whisper - audio -> text.

Contract (tools\\whisper\\README.md): transcribe.py <audio-file> -> transcript on
stdout, non-zero exit + stderr on failure. Accepts anything ffmpeg can read
(.ogg Discord voice notes, .mp3, .wav, .m4a, ...). Language auto-detected.
Default engine: local faster-whisper (installed by root install.bat).
"""
import importlib.util
import shutil
import subprocess
import sys
import types
from pathlib import Path


USAGE = ("usage: transcribe.py <audio-file>   -> transcript on stdout\n"
         "  env: WHISPER_MODEL=tiny|base|small|medium|large-v3  (default base)\n"
         "  accepts anything ffmpeg reads: .ogg voice notes, .mp3, .wav, .m4a")


def load_whisper_model():
    """Import WhisperModel, routing around a blocked PyAV if we have to.

    Returns (WhisperModel, decode_here) - `decode_here` True means `av` is a
    stub and the CALLER must hand transcribe() a numpy array, because
    faster_whisper's own decoder is gone.

    Raises ImportError with a message worth reading.
    """
    try:
        from faster_whisper import WhisperModel
        return WhisperModel, False
    except ImportError as first:
        # NOT INSTALLED and INSTALLED-BUT-BROKEN are different problems and
        # both raise ImportError. Ask the loader which one this is: a missing
        # spec is a missing package, anything else is a package that is right
        # there and failed to load.
        if importlib.util.find_spec("faster_whisper") is None:
            # NAME THE INTERPRETER. The old message said "run install.bat",
            # which on 2026-08-12 was both wrong and expensive: install.bat HAD
            # run - into Python 3.11 - and miniconda later took the front of
            # PATH, so `python` became a different interpreter with none of the
            # tool deps. A voice note came back "not installed" on a machine
            # where it demonstrably was, and finding that out meant probing
            # three interpreters by hand. A missing import here almost never
            # means "never installed"; it means THIS python is not the one that
            # was installed into.
            raise ImportError(
                f"faster-whisper is not installed for {sys.executable}\n"
                f"  install it there:  \"{sys.executable}\" -m pip install "
                f"faster-whisper\n"
                f"  (if install.bat already ran, it used a different "
                f"interpreter - whichever `python` resolved to at the time)"
            ) from first
        # PyAV's bundled DLL is the one dependency an OS policy can veto:
        # 2026-08-31 Windows Smart App Control blocked `av\_core.pyd` on his
        # PC ("Una directiva de Control de aplicaciones bloquea este archivo")
        # and every voice note came back as "faster-whisper is not installed"
        # on a machine where it was. faster_whisper imports `av` ONLY in
        # audio.py, and only calls it inside decode_audio() - which
        # transcribe() skips entirely when handed a numpy array. So a stub
        # satisfies the import and ffmpeg does the decoding instead.
        if "av" not in str(first) and "_core" not in str(first):
            raise
        if not shutil.which("ffmpeg"):
            raise ImportError(
                f"faster-whisper is installed for {sys.executable} but PyAV "
                f"will not load:\n  {first}\nffmpeg would be the way around "
                f"it, and ffmpeg is not on PATH either. Install ffmpeg, or "
                f"unblock PyAV in Windows Security > App & browser control."
            ) from first
        sys.modules.setdefault("av", types.ModuleType("av"))
        try:
            from faster_whisper import WhisperModel
        except ImportError as second:
            raise ImportError(
                f"faster-whisper is installed for {sys.executable} but will "
                f"not import:\n  {second}"
            ) from second
        print(f"note: PyAV will not load ({first}); decoding with ffmpeg "
              f"instead", file=sys.stderr)
        return WhisperModel, True


def decode_with_ffmpeg(audio: Path, rate: int = 16000):
    """s16le mono at `rate`, as the float32 array faster-whisper wants."""
    import numpy as np
    raw = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(audio),
         "-f", "s16le", "-ac", "1", "-ar", str(rate), "-"],
        capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


def main():
    args = sys.argv[1:]
    # --help is the first thing anyone tries on an unfamiliar CLI. Treating it
    # as a filename answered "file not found: --help" AND exited 0 - a success
    # code for a nonsense error, which is worse than either alone.
    if args and args[0] in ("-h", "--help"):
        print(__doc__.strip() + "\n\n" + USAGE)
        return 0
    if len(args) != 1:
        print(USAGE, file=sys.stderr)
        return 2
    audio = Path(args[0])
    if not audio.exists():
        print(f"file not found: {audio}", file=sys.stderr)
        return 2
    try:
        WhisperModel, decode_here = load_whisper_model()
    except ImportError as exc:
        print(exc, file=sys.stderr)
        return 3
    import os
    cfg = {}
    try:                                   # config is a convenience, never a gate
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import omnius_config as ocfg
        cfg = ocfg.load("transcribe").get("whisper") or {}
    except Exception:                                            # noqa: BLE001
        pass
    # DEFAULT `base` - HIS CALL, 2026-08-18, made with the measurement in hand.
    # That day a real note came back from `base` as "Proderas, cuento 7, mas 7"
    # where `small` gave "Prueba, cuanto es 7 mas 7?", and he chose latency
    # anyway: "quality is not best, but enough" against ~3x the processing
    # time. Fair reading of a worst case - the clip was four seconds, with no
    # context and no glossary word; longer notes give Whisper far more to work
    # with.
    #
    # What the measurement really bought is the KNOB, not the default: one line
    # in config\transcribe.ini (`model = small`) upgrades every note on a
    # machine where a mishearing costs more than ten seconds, and the env var
    # still beats both, so a single tricky recording can be re-run without
    # editing anything.
    size = os.environ.get("WHISPER_MODEL") or str(cfg.get("model") or "base").strip()
    model = WhisperModel(size, device="cpu", compute_type="int8")
    # LANGUAGE is a cheap belt, NOT the fix - measured on the same clip rather
    # than assumed: `base`+`language=es` still produced the gibberish, and
    # `small` with auto-detect was already correct. So the model did the work.
    # It stays because it removes a guess on a few noisy seconds for free, and
    # because tools\transcribe\transcribe_long.py has passed `--language` since
    # it was written while this path - the one desks use - never did. Empty
    # means auto-detect, which is right until an owner states their language.
    lang = (os.environ.get("WHISPER_LANGUAGE")
            or str(cfg.get("language") or "").strip()) or None
    # initial_prompt biases decoding toward our vocabulary. Measured 2026-07-31 on
    # real voice notes that `base` had mangled: "a sonmio" -> "Omnius" and
    # "task steering" -> "Task Scheduler", for +0.5s. Keep this list TIGHT: a
    # prompt is a bias, and stuffing it makes Whisper insert words never said.
    prompt = os.environ.get("WHISPER_PROMPT") or (
        "Omnius, Discord, watchdog, daybook, fleet-status, orchestrator, Claude, "
        "Task Scheduler, Windows, GitHub, commit, push, backup, skills, Whisper, "
        "Remotion, Pexip, Firebase, The Campus, Colegia, <your-org>, PWA, WebRTC."
    )
    source = decode_with_ffmpeg(audio) if decode_here else str(audio)
    segments, _info = model.transcribe(source, vad_filter=True,
                                       language=lang,
                                       initial_prompt=prompt or None)
    print(" ".join(s.text.strip() for s in segments).strip())
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
